#! /usr/bin/env python
from __future__ import absolute_import, print_function

"""
A sample implementation of a single coinjoin script,
adapted from `sendpayment.py` in Joinmarket-Org/joinmarket.
For notes, see scripts/README.md; in particular, note the use
of "schedules" with the -S flag.
"""

import random
import sys
import threading
from optparse import OptionParser
from twisted.internet import reactor
import time
import os
import pprint

from jmclient import (Taker, load_program_config, get_schedule,
                              JMClientProtocolFactory, start_reactor,
                              validate_address, jm_single, WalletError,
                              choose_orders, choose_sweep_orders,
                              cheapest_order_choose, weighted_order_choose,
                              sync_wallet, RegtestBitcoinCoreInterface,
                              estimate_tx_fee, direct_send, get_wallet_cls,
                              BitcoinCoreWallet)
from twisted.python.log import startLogging
from jmbase.support import get_log, debug_dump_object, get_password
from cli_options import get_sendpayment_parser

log = get_log()

#CLI specific, so relocated here (not used by tumbler)
def pick_order(orders, n): #pragma: no cover
    print("Considered orders:")
    for i, o in enumerate(orders):
        print("    %2d. %20s, CJ fee: %6s, tx fee: %6d" %
              (i, o[0]['counterparty'], str(o[0]['cjfee']), o[0]['txfee']))
    pickedOrderIndex = -1
    if i == 0:
        print("Only one possible pick, picking it.")
        return orders[0]
    while pickedOrderIndex == -1:
        try:
            pickedOrderIndex = int(raw_input('Pick an order between 0 and ' +
                                             str(i) + ': '))
        except ValueError:
            pickedOrderIndex = -1
            continue

        if 0 <= pickedOrderIndex < len(orders):
            return orders[pickedOrderIndex]
        pickedOrderIndex = -1

def main():
    parser = get_sendpayment_parser()
    (options, args) = parser.parse_args()
    load_program_config()
    if options.schedule == '' and len(args) < 3:
        parser.error('Needs a wallet, amount and destination address')
        sys.exit(0)

    #without schedule file option, use the arguments to create a schedule
    #of a single transaction
    sweeping = False
    if options.schedule == '':
        #note that sendpayment doesn't support fractional amounts, fractions throw
        #here.
        amount = int(args[1])
        if amount == 0:
            sweeping = True
        destaddr = args[2]
        mixdepth = options.mixdepth
        addr_valid, errormsg = validate_address(destaddr)
        if not addr_valid:
            print('ERROR: Address invalid. ' + errormsg)
            return
        schedule = [[options.mixdepth, amount, options.makercount,
                     destaddr, 0.0, 0]]
    else:
        result, schedule = get_schedule(options.schedule)
        if not result:
            log.info("Failed to load schedule file, quitting. Check the syntax.")
            log.info("Error was: " + str(schedule))
            sys.exit(0)
        mixdepth = 0
        for s in schedule:
            if s[1] == 0:
                sweeping = True
            #only used for checking the maximum mixdepth required
            mixdepth = max([mixdepth, s[0]])

    wallet_name = args[0]

    #to allow testing of confirm/unconfirm callback for multiple txs
    if isinstance(jm_single().bc_interface, RegtestBitcoinCoreInterface):
        jm_single().bc_interface.tick_forward_chain_interval = 10
        jm_single().bc_interface.simulating = True
        jm_single().maker_timeout_sec = 15

    chooseOrdersFunc = None
    if options.pickorders:
        chooseOrdersFunc = pick_order
        if sweeping:
            print('WARNING: You may have to pick offers multiple times')
            print('WARNING: due to manual offer picking while sweeping')
    elif options.choosecheapest:
        chooseOrdersFunc = cheapest_order_choose
    else:  # choose randomly (weighted)
        chooseOrdersFunc = weighted_order_choose

    # Dynamically estimate a realistic fee if it currently is the default value.
    # At this point we do not know even the number of our own inputs, so
    # we guess conservatively with 2 inputs and 2 outputs each.
    if options.txfee == -1:
        options.txfee = max(options.txfee, estimate_tx_fee(2, 2,
                                                           txtype="p2sh-p2wpkh"))
        log.debug("Estimated miner/tx fee for each cj participant: " + str(
            options.txfee))
    assert (options.txfee >= 0)

    log.debug('starting sendpayment')

    if not options.userpcwallet:
        #maxmixdepth in the wallet is actually the *number* of mixdepths (so misnamed);
        #to ensure we have enough, must be at least (requested index+1)
        max_mix_depth = max([mixdepth+1, options.amtmixdepths])
        if not os.path.exists(os.path.join('wallets', wallet_name)):
            wallet = get_wallet_cls()(wallet_name, None, max_mix_depth, options.gaplimit)
        else:
            while True:
                try:
                    pwd = get_password("Enter wallet decryption passphrase: ")
                    wallet = get_wallet_cls()(wallet_name, pwd, max_mix_depth, options.gaplimit)
                except WalletError:
                    print("Wrong password, try again.")
                    continue
                except Exception as e:
                    print("Failed to load wallet, error message: " + repr(e))
                    sys.exit(0)
                break
    else:
        wallet = BitcoinCoreWallet(fromaccount=wallet_name)
    if jm_single().config.get("BLOCKCHAIN",
        "blockchain_source") == "electrum-server" and options.makercount != 0:
        jm_single().bc_interface.synctype = "with-script"
    #wallet sync will now only occur on reactor start if we're joining.
    sync_wallet(wallet, fast=options.fastsync)
    if options.makercount == 0:
        if isinstance(wallet, BitcoinCoreWallet):
            raise NotImplementedError("Direct send only supported for JM wallets")
        direct_send(wallet, amount, mixdepth, destaddr, options.answeryes)
        return

    def filter_orders_callback(orders_fees, cjamount):
        orders, total_cj_fee = orders_fees
        log.info("Chose these orders: " +pprint.pformat(orders))
        log.info('total cj fee = ' + str(total_cj_fee))
        total_fee_pc = 1.0 * total_cj_fee / cjamount
        log.info('total coinjoin fee = ' + str(float('%.3g' % (
            100.0 * total_fee_pc))) + '%')
        WARNING_THRESHOLD = 0.02  # 2%
        if total_fee_pc > WARNING_THRESHOLD:
            log.info('\n'.join(['=' * 60] * 3))
            log.info('WARNING   ' * 6)
            log.info('\n'.join(['=' * 60] * 1))
            log.info('OFFERED COINJOIN FEE IS UNUSUALLY HIGH. DOUBLE/TRIPLE CHECK.')
            log.info('\n'.join(['=' * 60] * 1))
            log.info('WARNING   ' * 6)
            log.info('\n'.join(['=' * 60] * 3))
        if not options.answeryes:
            if raw_input('send with these orders? (y/n):')[0] != 'y':
                return False
        return True

    def taker_finished(res, fromtx=False, waittime=0.0, txdetails=None):
        if fromtx == "unconfirmed":
            #If final entry, stop *here*, don't wait for confirmation
            if taker.schedule_index + 1 == len(taker.schedule):
                reactor.stop()
            return
        if fromtx:
            if res:
                txd, txid = txdetails
                taker.wallet.remove_old_utxos(txd)
                taker.wallet.add_new_utxos(txd, txid)
                reactor.callLater(waittime*60,
                                  clientfactory.getClient().clientStart)
            else:
                #a transaction failed; we'll try to repeat without the
                #troublemakers
                #Note that Taker.nonrespondants is always set to the full maker
                #list at the start of Taker.receive_utxos, so it is always updated
                #to a new list in each run.
                print("We failed to complete the transaction. The following "
                      "makers didn't respond: ", taker.nonrespondants,
                      ", so we will retry without them.")
                taker.add_ignored_makers(taker.nonrespondants)
                #Now we have to set the specific group we want to use, and hopefully
                #they will respond again as they showed honesty last time.
                taker.add_honest_makers(list(set(
                    taker.maker_utxo_data.keys()).symmetric_difference(
                        set(taker.nonrespondants))))
                if len(taker.honest_makers) == 0:
                    log.info("None of the makers responded honestly; "
                             "giving up this attempt.")
                    reactor.stop()
                    return
                #we must reset the number of counterparties, as well as fix who they
                #are; this is because the number is used to e.g. calculate fees.
                #cleanest way is to reset the number in the schedule before restart.
                taker.schedule[taker.schedule_index][2] = len(taker.honest_makers)
                log.info("Retrying with: " + str(taker.schedule[
                    taker.schedule_index][2]) + " counterparties.")
                #rewind to try again (index is incremented in Taker.initialize())
                taker.schedule_index -= 1
                taker.set_honest_only(True)
                reactor.callLater(5.0, clientfactory.getClient().clientStart)
        else:
            if not res:
                log.info("Did not complete successfully, shutting down")
            #Should usually be unreachable, unless conf received out of order;
            #because we should stop on 'unconfirmed' for last (see above)
            else:
                log.info("All transactions completed correctly")
            reactor.stop()

    taker = Taker(wallet,
                  schedule,
                  order_chooser=chooseOrdersFunc,
                  callbacks=(filter_orders_callback, None, taker_finished))
    clientfactory = JMClientProtocolFactory(taker)
    nodaemon = jm_single().config.getint("DAEMON", "no_daemon")
    daemon = True if nodaemon == 1 else False
    if jm_single().config.get("BLOCKCHAIN", "network") in ["regtest", "testnet"]:
        startLogging(sys.stdout)
    start_reactor(jm_single().config.get("DAEMON", "daemon_host"),
                  jm_single().config.getint("DAEMON", "daemon_port"),
                  clientfactory, daemon=daemon)

if __name__ == "__main__":
    main()
    print('done')
