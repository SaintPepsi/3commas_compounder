'''
3commas deal compounder, grabs only the active deals that are either single or multibot.
'''

import math
import json
import configparser

from os.path import exists

# AWS
import boto3

# Misc
from py3cw.request import Py3CW

# Local packages
import logger
import utils
import webhook


# Parse/Read config.ini
config = configparser.ConfigParser()
additional_headers={'Forced-Mode': 'real'}


# need to make this os.getenv
try:
    # Try to get config.ini if it's present then we can assume we are running locally.
    config.read('config.ini')
    test_mode = config.get('run_mode', 'test')
    BOTS_CONFIG_LOCATION = 'bot_config/bots.json'
    LOCAL = 'True'

except configparser.NoSectionError:
    # If config.ini run_mode doesn't work:
    # We are most likely running in AWS Lambda
    # We need to use the lambda config.
    config.read('config.lambda.ini')
    test_mode = config.get('run_mode', 'test')
    BOTS_CONFIG_LOCATION = '/tmp/bots.json'
    with open(BOTS_CONFIG_LOCATION, 'wb') as f:
        boto3.client('s3').download_fileobj('3commas-compounder-data-bucket', 'bots.json', f)
    LOCAL = 'False'


# Check if local or AWS
if LOCAL == 'True':
    # Running locally, get secrets from config.ini
    secrets_dict = {
        "3commas_key": config.get('3commas', 'key'),
        "3commas_secret": config.get('3commas', 'secret'),
        "webhook_url": config.get('discord', 'webhook_url')
    }
else:
    # Running in AWS Lambda, get secrets from system manager
    boto_client = boto3.client("ssm")
    secret_parameters = [
        "/3commas-compounder/3commas_key",
        "/3commas-compounder/3commas_secret",
    ]

    secret_parameters_result = boto_client.get_parameters(
        Names=secret_parameters,
        WithDecryption=True,
    )
    secrets_dict = utils.parameter_dict_getter(secret_parameters_result)


# connect 3commas python wrapper
py3cw_request_options = {
        'request_timeout': 10,
        'nr_of_retries': 1,
        'retry_status_codes': [502]
    }
p3cw = Py3CW(
        key=secrets_dict["3commas_key"],
        secret=secrets_dict["3commas_secret"],
        request_options=py3cw_request_options
    )

def refresh_balances(account_id, forced_mode):
    '''
    Refresh the balance 3c has for the given exchange
    :param account_id: id of exchange account on 3c
    :return:
    '''
    error, _ = p3cw.request(
        entity='accounts',
        action='load_balances',
        action_id=str(account_id),
        additional_headers={'Forced-Mode': forced_mode}
    )

    if error:
        webhook.notify_webhook(error, 'ERROR')

currency_limit_adjuster = {
    "BTC": 0.00015,
    "BUSD": 11,
    "USDT": 11
}

def get_3c_currency_limit(bot_json):
    '''
    Helper function to get minimum BO amount for provided pair for DCA bot on 3c
    :param pair: pair we are looking up
    :return: tuple with (min BO, min price step size).
        Uses lotStep instead of priceStep
        this ensures we are using multiples the coin can also be sold at
    '''

    error, pair_limits = p3cw.request(
        entity='accounts',
        action='currency_rates',
        action_id='',
        payload={
            "market_code": bot_json['market_code'],
            "pair": bot_json['pairs'][0]
        },
        additional_headers=additional_headers
    )

    if error:
        webhook.notify_webhook(error, 'ERROR')

    # This 3c Endpoint will not work when using short bots
    # because those pairs (i.e ETH_USD) may not exist on the exchange
    # Workaround to start very small.
    # TODO: Check if this is working for short bots.
    # Could potentially lead to a sell amount that is not allowed on the exchange
    # since we dont have lotStep to reference
    # If so, may need to disable short bots from this script
    coins = bot_json['pairs'][0].split('_')
    if bot_json['currency'] != coins[0]:
        return (.001, .0001)


    min_total = float(pair_limits['minTotal'])

    logger.log(f"bot_json['pairs'] {bot_json['pairs']}", "INFO")
    logger.log(f"min_total {min_total}", "INFO")

    if coins[0] in currency_limit_adjuster:
        currency_minimal = currency_limit_adjuster[coins[0]]
        if min_total < currency_minimal:
            return currency_minimal

    return min_total


def update_bot(bot_id, valid_bo, valid_so, valid_mad, valid_adosp, bot_json, forced_mode):
    '''
    Helper function to hit the 3c api and update the bot's bo and so
    :param account_id: account id the bot is on
    :param bot_id: id of the bot
    :param valid_bo: auto generated BO
    :param valid_so: auto generated SO
    :param bot_json: other required params we are not modifying but are required by 3c endpoint
    :return:
    '''

    if test_mode == 'True':
        logger.log("Test Run Completed!", "INFO")
    else:
        error, updated_bot = p3cw.request(
            entity='bots',
            action='update',
            action_id=str(bot_id),
            payload={
                'name': bot_json['name'],
                'pairs': bot_json['pairs'],
                # this is auto calculated value that we're changing
                'base_order_volume': f'{valid_bo}',
                'take_profit': bot_json['tp'],
                # this is auto calculated value that we're changing
                'safety_order_volume': f'{valid_so}',
                'martingale_volume_coefficient': bot_json['os'],
                'martingale_step_coefficient': bot_json['ss'],
                'max_safety_orders': bot_json['mstc'],
                'active_safety_orders_count': bot_json['active_safety_orders_count'],
                'safety_order_step_percentage': bot_json['sos'],
                'take_profit_type': bot_json['take_profit_type'],
                'strategy_list': bot_json['strategy_list'],
                'bot_id': bot_id,
                'max_active_deals': valid_mad,
                'allowed_deals_on_same_pair': valid_adosp,
            },
            additional_headers={'Forced-Mode': forced_mode}
        )
        if error == {}:
            info_message = f"{bot_json['name']} Updated!"
            logger.log(info_message, "INFO")
            debug_updated_bot = f"{updated_bot}"
            logger.log(debug_updated_bot, "INFO")
        else:
            webhook.notify_webhook(f"{bot_json['name']} NOT completed:\n{error['msg']}", 'ERROR')


def calc_max_funds_per_deal(
        bo: float,
        so: float,
        mstc: int,
        sos: float,
        os: float,
        ss: float
    ) -> float:
    '''
    Helper function to optimize allocations on
    :param bo: Base Order
    :param so: Safety Order
    :param mstc: Max Safety Trade Count
    :param sos: Safety Order Step
    :param os: (Safety) Order Scale
    :param ss: (Safety Order) Step Scale
    :return: max funds the bot can use
    '''
    max_total = bo
    drawdown = .0
    # stc indexed from 0
    for stc in range(0, mstc):
        drawdown += sos * ss ** stc
        if drawdown >= 100:  # TODO: Validate that using 100 here instead of 1 is correct
            return max_total
        max_total += so * os ** stc
    return max_total


def get_currency(pair, strategy, volume_type):
    '''
    Helper function to grab currency used for the bot
    :param pair: pair the bot is trading
    :param strategy: long/short
    :param volume_type: base/quote
    :return:
    '''
    _pair = pair.split('_')
    # if strategy == 'long':
    #     return _pair[0] if volume_type == 'quote_currency' else _pair[1]
    # else:
    #     return _pair[1] if volume_type == 'quote_currency' else _pair[0]
    return _pair[0] if volume_type == 'quote_currency' else _pair[1]

def fetch_bots_for_accounts(account_config_dict, forced_mode):
    '''
    Function to gather all bots for accounts.
    :param account_config_dict: config dict to carry all account/bot data
    :param forced_mode: 'real' or 'paper' trading.
    '''
    logger.log('Pulling bot info...', "INFO")
    ## Get list of all enabled bots to find out which accounts/currencies are needed to be optimized
    # infinite bots while loop
    bot_offset = 0
    bot_limit = 100

    keep_fetching_bots = True

    while keep_fetching_bots:
        error, bots = p3cw.request(
            entity='bots',
            action='',
            payload={
                "scope": "enabled",
                "limit": f"{bot_limit}",
                "offset":f"{bot_offset}"
            },
            additional_headers={'Forced-Mode': forced_mode}
        )

        if error:
            webhook.notify_webhook(error, 'ERROR')

        # No more bots or less than needed for an additional loop.
        if len(bots) == 0 or len(bots) < 100:
            keep_fetching_bots = False
        else:
            # Potentially more bots avaialable.
            bot_offset += bot_limit

        for bot in bots:
            # Only support long bots that buy in quote and short bots that sell in base
            if (
                bot['strategy'] == 'long' and
                (
                    bot['base_order_volume_type'] != 'quote_currency' or
                    bot['safety_order_volume_type'] != 'quote_currency'
                )
            ) or (
                bot['strategy'] == 'short' and
                (
                    bot['base_order_volume_type'] != 'base_currency' or
                    bot['safety_order_volume_type'] != 'base_currency'
                )
            ):
                bot_support_warning = (
                    'Only Long Quote and Short Base bots are supported. '
                    f'Skipping {bot["name"]}')
                logger.log(bot_support_warning, "WARNING")
                continue

            # If account not already in config_dict, add it
            account_id = bot['account_id']

            if account_id not in account_config_dict['accounts']:
                account_config_dict['accounts'][account_id] = {}
                account_config_dict['accounts'][account_id]['forced_mode'] = forced_mode
                account_config_dict['accounts'][account_id]['account_name'] = bot['account_name']
                account_config_dict['accounts'][account_id]['balances'] = {}
                account_config_dict['accounts'][account_id]['bots'] = {}

            bot_id = bot['id']

            account_config_dict['accounts'][account_id]['bots'][bot_id] = {}

            # readibility short-cut
            bot_config_dict = account_config_dict['accounts'][account_id]['bots'][bot_id]
            # Add 3c settings
            bot_config_dict['name'] = bot['name']
            bot_config_dict['bo'] = bot['base_order_volume']
            bot_config_dict['so'] = bot['safety_order_volume']
            bot_config_dict['os'] = bot['martingale_volume_coefficient']
            bot_config_dict['ss'] = bot['martingale_step_coefficient']
            bot_config_dict['mad'] = bot['max_active_deals']
            bot_config_dict['adosp'] = bot['allowed_deals_on_same_pair']
            bot_config_dict['sos'] = bot['safety_order_step_percentage']
            bot_config_dict['mstc'] = bot['max_safety_orders']
            bot_config_dict['pairs'] = bot['pairs']
            bot_config_dict['type'] = bot['type']
            bot_config_dict['active_safety_orders_count'] = bot['active_safety_orders_count']
            bot_config_dict['take_profit_type'] = bot['take_profit_type']
            bot_config_dict['tp'] = bot['take_profit']
            bot_config_dict['strategy_list'] = bot['strategy_list']

            # Get the currency used for the deal
            currency = get_currency(bot['pairs'][0], bot['strategy'], bot['base_order_volume_type'])
            account_config_dict['accounts'][account_id]['bots'][bot_id]['currency'] = currency

            # If currency not already in the account, add it
            account_config_dict['accounts'][account_id]['balances'][currency] = 0.0

            # Add market code so we can look up currency limits for that exchange later
            error, account_info = p3cw.request(
                entity='accounts',
                action='account_info',
                action_id=str(account_id),
                additional_headers={'Forced-Mode': forced_mode}
            )
            if error:
                webhook.notify_webhook(
                    (
                        f'Error getting account info for [{bot["account_name"]}]'
                        f'(https://3commas.io/accounts/{bot["account_id"]})'
                    ),
                    'ERROR'
                )

            bot_config_dict['market_code'] = account_info['market_code']


def get_config():
    '''
    Pulls necessary information from 3c api to generate config files
    :return:
    '''
    config_dict = {"accounts": {}}
    fetch_bots_for_accounts(account_config_dict=config_dict, forced_mode='real')
    fetch_bots_for_accounts(account_config_dict=config_dict, forced_mode='paper')

    logger.log('Pulling account balances...', "INFO")

    # Get balance for every currency for each account
    for account_id, account in config_dict['accounts'].items():
        # Refresh the balance 3c has for the exchange
        forced_mode = account['forced_mode']

        refresh_balances(account_id, forced_mode=forced_mode)

        error, account_balances = p3cw.request(
            entity='accounts',
            action='account_table_data',
            action_id=str(account_id),
            payload={
                "account_id": account_id
            },
            additional_headers={'Forced-Mode': forced_mode}
        )

        if error:
            # print(error)
            webhook.notify_webhook(
                (
                    'Error getting account table data for '
                    f'[{account_id}](https://3commas.io/accounts/{account_id})'
                ),
                'ERROR'
            )

        # Update balances we have bots using for the given account
        for pair in account_balances:
            config_dict_account_balances = config_dict['accounts'][account_id]['balances']
            if pair['currency_code'] in config_dict_account_balances:
                config_dict_account_balances[pair['currency_code']] += float(pair['equity'])


    # Add in deal balances for bots that can get compounded
    for account_id in config_dict['accounts']:
        for bot_id in config_dict['accounts'][account_id]['bots']:

            forced_mode = config_dict['accounts'][account_id]['forced_mode']

            error, active_deals = p3cw.request(
                entity='deals',
                action='',
                payload={
                    "account_id": account_id,
                    "scope": "active",
                    "bot_id": bot_id
                },
            )
            if error:
                logger.log(str(error), "ERROR")
                if LOCAL == 'False' and forced_mode == 'paper':
                    continue

                webhook.notify_webhook(
                    (
                        'Error getting active deals for:\n'
                        f'Account: [{account_id}](https://3commas.io/accounts/{account_id})\n'
                        f'Bot: [{bot_id}](https://3commas.io/bots/{bot_id})\n'
                        f'Error code: [{error["status_code"]}]\n'
                        f'Forced Mode: {forced_mode}'
                    ),
                    'ERROR'
                )
                continue

            for deal in active_deals:
                currency_code = get_currency(
                    deal['pair'],
                    deal['strategy'],
                    deal['base_order_volume_type']
                )
                config_dict_account_balances = config_dict['accounts'][account_id]['balances']
                if currency_code in config_dict_account_balances:
                    currency_amount = {
                        'long': 'bought_volume',
                        'short': 'sold_amount'
                    }
                    strat_key = currency_amount[deal['strategy']]
                    config_dict_account_balances[currency_code] += float(deal[strat_key])

    return config_dict

def create_user_config(auto_config):
    '''
    Creates bots.json file for user to input allocations
    :param auto_config:
    :return:
    '''
    user_conf = auto_config
    for account_id in user_conf['accounts'].keys():
        user_conf['accounts'][account_id]['currencies'] = {}
        # Make a group for each currency and
        for currency in user_conf['accounts'][account_id]['balances'].keys():
            user_conf['accounts'][account_id]['currencies'][currency] = {}
        # clean up unneeded balances
        del user_conf['accounts'][account_id]['balances']
        # Add the bots to their respective groups
        for bot_id in user_conf['accounts'][account_id]['bots']:
            # Get the currency of the bot
            config_bot = user_conf['accounts'][account_id]['bots'][bot_id]
            _currency = config_bot['currency']
            # Create a dict in the currency dict for the bot
            user_conf['accounts'][account_id]['currencies'][_currency][bot_id] = {}
            user_conf_bot_dict = user_conf['accounts'][account_id]['currencies'][_currency][bot_id]
            # Add name of the bot and key for % allocation
            user_conf_bot_dict['bot_name'] = \
                config_bot['name']
            # add % allocation key for user to input
            user_conf_bot_dict['allocation'] = None
            # add max active deals key for user to input
            if config_bot['type'] == "Bot::MultiBot":
                user_conf_bot_dict['max_active_deals'] = config_bot['mad']


        # clean up uneeded bots key
        del user_conf['accounts'][account_id]['bots']

        # Autofill with 100% if only 1 bot for the currency on the account
        currencies = user_conf['accounts'][account_id]['currencies']
        for currency in currencies:
            bot_ids = list(currencies[currency].keys())
            if len(bot_ids) == 1:
                currencies[currency][bot_ids[0]]['allocation'] = 1.0

    # Write config to file
    with open(BOTS_CONFIG_LOCATION, "w", encoding='UTF-8') as outfile:
        json.dump(user_conf, outfile, indent=4)


def check_user_config(bot_user_config):
    '''
    Checks that the bots.json file is configured and has all necessary data for script to run
    :return: valid user config
    '''

    # Check if the bots.json file exists, if not create it and prompt user
    file_exists = exists(BOTS_CONFIG_LOCATION)

    if not file_exists:
        if LOCAL == 'True':
            create_user_config(bot_user_config)
            webhook.notify_webhook(
                (
                    f'Could not find a `bots.json` in {BOTS_CONFIG_LOCATION}, '
                    'please populate the newly created file.'
                ),
                'ERROR'
            )
        else:
            webhook.notify_webhook(
                (
                    f'Could not find a `bots.json` in {BOTS_CONFIG_LOCATION}, '
                    'please check s3 file download.'
                ),
                'ERROR'
            )
        return False
    # if bots.json file DOES exist,
    # make sure it has all of the bot ids and all allocations are defined
    with open(BOTS_CONFIG_LOCATION, "r", encoding='UTF-8') as infile:
        user_config = json.load(infile)
        bot_ids = []
        for account_id in user_config['accounts']:
            user_config_account = user_config['accounts'][account_id]
            account_name = user_config_account['account_name']
            for currency in user_config_account['currencies']:
                allocation = 0
                for bot_id in user_config['accounts'][account_id]['currencies'][currency]:
                    bot_ids.append(int(bot_id))
                    # If a bot doesn't have its allocation defined, break
                    bot_allocation = user_config_account['currencies'][currency][bot_id][
                        'allocation']
                    bot_name = user_config_account['currencies'][currency][bot_id]['bot_name']
                    if not bot_allocation:
                        webhook.notify_webhook(
                            (
                                f'{account_name} "{bot_name}" does not have an allocation defined'
                            ),
                            'ERROR'
                        )
                        return False
                    allocation += float(bot_allocation)
                # Throw warning if risk is greater than 100%
                if allocation > 1:
                    risk_factor_warning = (
                        f'{account_name} {currency} has a '
                        f'risk factor of {allocation * 100}'
                    )
                    logger.log(risk_factor_warning, "WARNING")

        # if there is a live bot that does not have a config in bots.json, break
        # print("bot_user_config['accounts']",bot_user_config['accounts'])
        for account_id in bot_user_config['accounts']:
            # If ingore other bots is active dont worry about checking other bots.
            # just use the bots that are there.

            if "ignore_other_bots" in user_config['accounts'][str(account_id)] and \
                user_config['accounts'][str(account_id)]["ignore_other_bots"] is True:
                continue

            for bot_id in bot_user_config['accounts'][account_id]['bots']:
                if bot_id not in bot_ids:
                    webhook.notify_webhook(
                        (
                            "bots.json is missing new bots. "
                            f"[{bot_id}](https://3commas.io/bots/{bot_id}) \n"
                            "Please delete the file and re-run this script"
                        ),
                        'ERROR'
                    )
                    return False
    return user_config


def optimize_bot(
        bot_id,
        bot_json,
        max_currency_allocated,
        bot_max_active_deals,
        bot_same_pair_multiple,
        forced_mode
    ):
    '''
    Helper function to find optimal bot settings and update 3c via api
    :param bot_id: 3c ID of the bot
    :param bot_json: bot settings to give to calc_max_funds_per_deal
    :param max_currency_allocated: total amount of funds we are allocating to the bot
    :return:
    '''
    max_currency_allocation_info = f'max_currency_allocated: {max_currency_allocated}'
    logger.log(max_currency_allocation_info, "INFO")


    # Get min BO and price step for currency on given exchange
    min_volume = get_3c_currency_limit(bot_json)

    logger.log(f"min_volume {min_volume}", "INFO")


    # Get ratio of BO:SO from bot settings
    boso_ratio = float(bot_json['bo']) / float(bot_json['so'])

    buy_order = min_volume if boso_ratio <= 1 else min_volume * boso_ratio
    # Maintain ratio user has in their settings
    safety_order = buy_order / boso_ratio if boso_ratio <= 1 else min_volume

    # Get minimum max_funds for bot settings to use as starting point
    mstc = int(bot_json['mstc']) # Max Safety Trades Count
    sos = float(bot_json['sos']) # Safety Order Scale
    order_scale = float(bot_json['os']) # Order Scale
    safety_scale = float(bot_json['ss']) # Safety Scale

    max_funds_per_deal = calc_max_funds_per_deal(
        bo=buy_order,
        so=safety_order,
        mstc=mstc,
        sos=sos,
        os=order_scale,
        ss=safety_scale
    )

    valid_bo = buy_order
    valid_so = safety_order
    valid_mad = 1
    valid_adosp = 1

    ### 09-02-2022 SanCoca BO SO MAD optimiser
    bot_type = bot_json["type"]
    bot_name = bot_json["name"]
    potential_max_deals = max_currency_allocated / max_funds_per_deal
    floor_max_deals = math.floor(potential_max_deals)

    bot_type_info = f'bot_type: {bot_type}'
    logger.log(bot_type_info, "INFO")
    floor_max_deals_info = f'floor_max_deals: {floor_max_deals}'
    logger.log(floor_max_deals_info, "INFO")

    # Make sure the bot can actually use 1 or more deals
    if floor_max_deals >= 1:
        if bot_type == "Bot::MultiBot":
            if potential_max_deals >= bot_max_active_deals:
                # Potential max deals is greater than we want it to be (6),
                # we need to increase BO:SO scale based on residual max deals
                # max_active_deals = 6
                # potential_max_deals = 8.6
                # remainig_deal_space = potential_max_deals - max_active_deals = 2.6
                # BO:SO scale increase = remaining_deal_space / max_active_deals = 0.433

                remainig_deal_space = potential_max_deals - bot_max_active_deals

                deal_bo_so_increase = remainig_deal_space / bot_max_active_deals

                valid_bo += buy_order * deal_bo_so_increase
                valid_so += safety_order * deal_bo_so_increase
                valid_mad = bot_max_active_deals
            else:
                # we are still under max active deals (6)
                # we need to scale based on what is left over
                # potential_max_deals = 3.8
                # max_deals = math.floor(potential_max_deals) = 3
                # remainig_deal_space = potential_max_deals - max_deals = 0.8
                # BO:SO scale increase = remaining_deal_space / max_deals = 0.26
                remainig_deal_space = potential_max_deals - floor_max_deals

                deal_bo_so_increase = remainig_deal_space / floor_max_deals

                valid_bo += buy_order * deal_bo_so_increase
                valid_so += safety_order * deal_bo_so_increase
                valid_mad = floor_max_deals

            remainig_deal_space_info = f'remainig_deal_space {remainig_deal_space}'
            logger.log(remainig_deal_space_info, "INFO")

            deal_bo_so_increase_info = f'deal_bo_so_increase {deal_bo_so_increase}'
            logger.log(deal_bo_so_increase_info, "INFO")
        elif bot_type == "Bot::SingleBot":
            valid_bo = buy_order * potential_max_deals
            valid_so = safety_order * potential_max_deals
    else:
        webhook.notify_webhook(
            (
                f'Not enough funds for 1 active deal, '
                'using minimum bo:so for bot: '
                f'[{bot_name}](https://3commas.io/bots/{bot_id})'
            ),
            'WARNING'
        )

    max_funds_per_deal_new_size = \
        calc_max_funds_per_deal(bo=valid_bo, so=valid_so, mstc=mstc, sos=sos, os=order_scale, ss=safety_scale)
    max_funds_per_deal_new_size_info = f'max_funds_per_deal_new_size: {max_funds_per_deal_new_size}'
    logger.log(max_funds_per_deal_new_size_info, "INFO")

    total_funds_used_by_bot = max_funds_per_deal_new_size * valid_mad
    total_funds_used_by_bot_info = f'total_funds_used_by_bot: {total_funds_used_by_bot}'
    logger.log(total_funds_used_by_bot_info, "INFO")

    # Round to max 8 the bo:so
    valid_bo = round(valid_bo, 8)
    valid_so = round(valid_so, 8)
    # Scale max allowed deals per pair based on mad
    if bot_same_pair_multiple:
        valid_adosp = math.ceil(valid_mad / bot_same_pair_multiple)


    if (
        float(bot_json['bo']) != valid_bo or
        float(bot_json['so']) != valid_so or
        bot_json['mad'] != valid_mad or
        bot_json['adosp'] != valid_adosp
    ):
        # Only Send api requests to 3c to update the bot if the data is different than what it was.
        optimal_found_info = (
            f'Optimal settings for {bot_json["name"]} ({bot_id}) found! '
            f'BO: {round(valid_bo, 8)}, SO: {round(valid_so, 8)}, '
            f'MAD: {valid_mad}, ADOSP: {valid_adosp}'
        )
        logger.log(optimal_found_info, "INFO")

        update_bot(
            bot_id,
            valid_bo,
            valid_so,
            valid_mad,
            valid_adosp,
            bot_json,
            forced_mode=forced_mode
        )
    else:
        # Did not find newer settings
        no_optimal_found_info = (
            'Could not find new optimal settings for '
            f'{bot_json["name"]} ({bot_id}).'
        )
        logger.log(no_optimal_found_info, "INFO")



def compounder_start():
    '''
    Compounder start method. this starts all the other
    '''
    # Get bot configs from 3c
    bot_config = get_config()
    # Compare 3c live configs against user config (bots.json)
    user_config = check_user_config(bot_config)

    # If configs are good, update bots
    if user_config:
        logger.log('Valid config found, proceeding to update bots...', "INFO")
        # Update the bots
        for account_id in bot_config['accounts']:
            # Get account balances
            account_balances = bot_config['accounts'][account_id]['balances']
            # Loop through each bot to multiply the allocation against total balance
            # for the accounts currency balance
            for bot_id in bot_config['accounts'][account_id]['bots']:
                bot_json = bot_config['accounts'][account_id]['bots'][bot_id]
                bot_currency = bot_json['currency']

                config_account = user_config['accounts'][str(account_id)]
                if bot_currency not in config_account['currencies']:
                    continue

                if str(bot_id) not in config_account['currencies'][bot_currency]:
                    print("config_account['currencies'][bot_currency]", config_account['currencies'][bot_currency])
                    print('bot_id', bot_id)
                    continue

                # Print account balance
                account_currency_balance_info = (
                    f"{bot_config['accounts'][account_id]['account_name']} "
                    f"{bot_currency} balance: {account_balances[bot_currency]}"
                )
                logger.log(account_currency_balance_info, "INFO")


                # Get the allocation for the bot from user_config
                user_conf_bot = config_account['currencies'][bot_currency][str(bot_id)]

                bot_allocation = user_conf_bot['allocation']
                # check if bot has Max active deal, if it does use it otherwise set it to 1
                bot_max_active_deals = 1 \
                    if not "max_active_deals" in user_conf_bot \
                    else user_conf_bot['max_active_deals']

                # allow multiple deals with same pair
                bot_same_pair_multiple = False \
                    if not "bot_same_pair_multiple" in user_conf_bot \
                    else user_conf_bot['bot_same_pair_multiple']

                max_currency_allocated = \
                    float(account_balances[bot_currency]) * float(bot_allocation)

                allocation_allowance_info = (
                    "Allocation allowed: "
                    f"{max_currency_allocated} {bot_currency}"
                )
                logger.log(allocation_allowance_info, "INFO")



                # Pass the settings to optimize function to find optimal BO:SO for allocation
                optimize_bot(
                    bot_id=bot_id,
                    bot_json=bot_json,
                    max_currency_allocated=max_currency_allocated,
                    bot_max_active_deals=bot_max_active_deals,
                    bot_same_pair_multiple=bot_same_pair_multiple,
                    forced_mode=bot_config['accounts'][account_id]['forced_mode']
                )
#

def request_handler(event, lambda_context):
    '''
    Lambda request handler to / entry for lambda
    '''
    compounder_start()


if __name__ == "__main__":

    compounder_start()
