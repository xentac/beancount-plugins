"""Compute unrealized gains.

The configuration for this plugin is a single string, the name of the subaccount
to add to post the unrealized gains to, like this:

  plugin "beancount.plugins.unrealized" "Unrealized"

If you don't specify a name for the subaccount (the configuration value is
optional), by default it inserts the unrealized gains in the same account that
is being adjusted.

Modified by Jason Chu <xentac@gmail.com> to post new transactions every month
"""
__author__ = "Martin Blais <blais@furius.ca>"

import collections
import datetime

from beancount.core.number import ZERO
from beancount.core import data
from beancount.core import account
from beancount.core import getters
from beancount.core import amount
from beancount.core import flags
from beancount.ops import holdings
from beancount.core import prices
from beancount.ops import summarize
from beancount.parser import options
from beancount.utils import date_utils


__plugins__ = ('add_unrealized_gains',)


UnrealizedError = collections.namedtuple('UnrealizedError', 'source message entry')


ONEDAY = datetime.timedelta(days=1)


def matching_unrealized_transaction(entry, account, cost_currency, prev_currency):
    return (any(posting.account == account for posting in entry.postings) and
            entry.postings[0].units.currency == cost_currency and
            entry.meta['prev_currency'] == prev_currency)


def find_previous_unrealized_transaction(entries, account, cost_currency, prev_currency, include_clear=False):
    for entry in reversed(entries):
        if matching_unrealized_transaction(entry, account, cost_currency, prev_currency):
            if not include_clear and entry.narration.startswith('Clear unrealized'):
                return None
            return entry
    return None


def add_unrealized_gains_at_date(entries, unrealized_entries, income_account_type,
                                 price_map, date, meta, subaccount):
    """Insert/remove entries for unrealized capital gains 

    This function takes a list of entries and a date and creates a set of unrealized gains
    transactions, negating previous unrealized gains transactions within the same account.

    Args:
      entries: A list of data directives.
      unrealized_entries: A list of previously generated unrealized transactions.
      income_account_type: The income account type.
      price_map: A price map returned by prices.build_price_map.
      date: The effective date to generate the unrealized transactions for.
      meta: meta.
      subaccount: A string, and optional the name of a subaccount to create
        under an account to book the unrealized gain. If this is left to its
        default value, the gain is booked directly in the same account.
    Returns:
      A list of newly created unrealized transactions and a list of errors.
    """
    errors = []

    entries_truncated = summarize.truncate(entries, date + ONEDAY)

    holdings_list = holdings.get_final_holdings(entries_truncated, price_map=price_map, date=date)

    # Group positions by (account, cost, cost_currency).
    holdings_list = holdings.aggregate_holdings_by(
        holdings_list, lambda h: (h.account, h.currency, h.cost_currency))

    holdings_with_currencies = set()

    # Create transactions to account for each position.
    new_entries = []
    for index, holding in enumerate(holdings_list):
        if (holding.currency == holding.cost_currency or
            holding.cost_currency is None):
            continue

        # Note: since we're only considering positions held at cost, the
        # transaction that created the position *must* have created at least one
        # price point for that commodity, so we never expect for a price not to
        # be available, which is reasonable.
        if holding.price_number is None:
            # An entry without a price might indicate that this is a holding
            # resulting from leaked cost basis. {0ed05c502e63, b/16}
            if holding.number:
                errors.append(
                    UnrealizedError(meta,
                                    "A valid price for {h.currency}/{h.cost_currency} "
                                    "could not be found".format(h=holding), None))
            continue

        # Compute the PnL; if there is no profit or loss, we create a
        # corresponding entry anyway.
        pnl = holding.market_value - holding.book_value
        if holding.number == ZERO:
            # If the number of units sum to zero, the holdings should have been
            # zero.
            errors.append(
                UnrealizedError(
                    meta,
                    "Number of units of {} in {} in holdings sum to zero "
                    "for account {} and should not".format(
                        holding.currency, holding.cost_currency, holding.account),
                    None))
            continue

        # Compute the name of the accounts and add the requested subaccount name
        # if requested.
        asset_account = holding.account
        income_account = account.join(income_account_type,
                                      account.sans_root(holding.account))
        if subaccount:
            asset_account = account.join(asset_account, subaccount)
            income_account = account.join(income_account, subaccount)

        holdings_with_currencies.add((holding.account, holding.cost_currency, holding.currency))

        # Find the previous unrealized gain entry to negate and decide if we
        # should create a new posting.
        latest_unrealized_entry = find_previous_unrealized_transaction(unrealized_entries, asset_account, holding.cost_currency, holding.currency)

        # Don't create a new transaction if our last one hasn't changed.
        if (latest_unrealized_entry and
            pnl == latest_unrealized_entry.postings[0].units.number):
            continue

        # Don't bother creating a blank unrealized transaction if none existed
        if pnl == ZERO and not latest_unrealized_entry:
            continue

        relative_pnl = pnl
        if latest_unrealized_entry:
            relative_pnl = pnl - latest_unrealized_entry.postings[0].units.number

        # Create a new transaction to account for this difference in gain.
        gain_loss_str = "gain" if relative_pnl > ZERO else "loss"
        narration = ("Unrealized {} for {h.number} units of {h.currency} "
                     "(price: {h.price_number:.4f} {h.cost_currency} as of {h.price_date}, "
                     "average cost: {h.cost_number:.4f} {h.cost_currency})").format(
                         gain_loss_str, h=holding)
        entry = data.Transaction(data.new_metadata(meta["filename"], lineno=1000 + index,
                                 kvlist={'prev_currency': holding.currency}), date,
                                 flags.FLAG_UNREALIZED, None, narration, set(), set(), [])

        # Book this as income, converting the account name to be the same, but as income.
        # Note: this is a rather convenient but arbitraty choice--maybe it would be best to
        # let the user decide to what account to book it, but I don't a nice way to let the
        # user specify this.
        #
        # Note: we never set a price because we don't want these to end up in Conversions.
        entry.postings.extend([
            data.Posting(
                asset_account,
                amount.Amount(pnl, holding.cost_currency),
                None,
                None,
                None,
                None),
            data.Posting(
                income_account,
                amount.Amount(-pnl, holding.cost_currency),
                None,
                None,
                None,
                None)
        ])
        if latest_unrealized_entry:
            for posting in latest_unrealized_entry.postings[:2]:
                entry.postings.append(
                    data.Posting(
                        posting.account,
                        -posting.units,
                        None,
                        None,
                        None,
                        None))

        new_entries.append(entry)

    return new_entries, holdings_with_currencies, errors


def add_unrealized_gains(entries, options_map, subaccount=None):
    """Insert entries for unrealized capital gains.

    This function inserts entries that represent unrealized gains, at the end of
    the available history. It returns a new list of entries, with the new gains
    inserted. It replaces the account type with an entry in an income account.
    Optionally, it can book the gain in a subaccount of the original and income
    accounts.

    Args:
      entries: A list of data directives.
      options_map: A dict of options, that confirms to beancount.parser.options.
      subaccount: A string, and optional the name of a subaccount to create
        under an account to book the unrealized gain. If this is left to its
        default value, the gain is booked directly in the same account.
    Returns:
      A list of entries, which includes the new unrealized capital gains entries
      at the end, and a list of errors. The new list of entries is still sorted.
    """
    errors = []
    meta = data.new_metadata('<unrealized_gains>', 0)

    account_types = options.get_account_types(options_map)

    # Assert the subaccount name is in valid format.
    if subaccount:
        validation_account = account.join(account_types.assets, subaccount)
        if not account.is_valid(validation_account):
            errors.append(
                UnrealizedError(meta,
                                "Invalid subaccount name: '{}'".format(subaccount),
                                None))
            return entries, errors

    if not entries:
        return (entries, errors)

    # Group positions by (account, cost, cost_currency).
    price_map = prices.build_price_map(entries)

    new_entries = []

    # Start at the first month after our first transaction
    date = date_utils.next_month(entries[0].date)
    last_month = date_utils.next_month(entries[-1].date)
    last_holdings_with_currencies = None
    while date <= last_month:
        date_entries, holdings_with_currencies, date_errors = add_unrealized_gains_at_date(
            entries, new_entries, account_types.income, price_map, date, meta,
            subaccount)
        new_entries.extend(date_entries)
        errors.extend(date_errors)

        if last_holdings_with_currencies:
            for account_, cost_currency, currency in last_holdings_with_currencies - holdings_with_currencies:
                # Create a negation transaction specifically to mark that all gains have been realized
                if subaccount:
                    account_ = account.join(account_, subaccount)

                latest_unrealized_entry = find_previous_unrealized_transaction(new_entries, account_, cost_currency, currency)
                if not latest_unrealized_entry:
                    continue
                entry = data.Transaction(data.new_metadata(meta["filename"], lineno=999,
                                         kvlist={'prev_currency': currency}), date,
                                         flags.FLAG_UNREALIZED, None, 'Clear unrealized gains/losses of {}'.format(currency), set(), set(), [])

                # Negate the previous transaction because of unrealized gains are now 0
                for posting in latest_unrealized_entry.postings[:2]:
                    entry.postings.append(
                        data.Posting(
                            posting.account,
                            -posting.units,
                            None,
                            None,
                            None,
                            None))
                new_entries.append(entry)


        last_holdings_with_currencies = holdings_with_currencies
        date = date_utils.next_month(date)

    # Ensure that the accounts we're going to use to book the postings exist, by
    # creating open entries for those that we generated that weren't already
    # existing accounts.
    new_accounts = {posting.account
                    for entry in new_entries
                    for posting in entry.postings}
    open_entries = getters.get_account_open_close(entries)
    new_open_entries = []
    for index, account_ in enumerate(sorted(new_accounts)):
        if account_ not in open_entries:
            meta = data.new_metadata(meta["filename"], index)
            open_entry = data.Open(meta, new_entries[0].date, account_, None, None)
            new_open_entries.append(open_entry)

    return (entries + new_open_entries + new_entries, errors)


def get_unrealized_entries(entries):
    """Return entries automatically created for unrealized gains.

    Args:
      entries: A list of directives.
    Returns:
      A list of directives, all of which are in the original list.
    """
    return [entry
            for entry in entries
            if (isinstance(entry, data.Transaction) and
                entry.flag == flags.FLAG_UNREALIZED)]
