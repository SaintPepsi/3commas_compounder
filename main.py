import math   
import logging
from py3cw.request import Py3CW
import configparser
import json
import requests
from os.path import exists
# AWS 
import boto3

# Parse/Read config.ini
config = configparser.ConfigParser()
bots_config_location = 'bot_config/bots.json'

try:
    # Try to get config.ini if it's present then we can assume we are running locally.
    config.read('config.ini')
    test_mode = config.get('run_mode', 'test')
    local = 'True'
except:
    # If config.ini run_mode doesn't work we are most likely running in AWS Lambda so we need to use the lambda config.
    config.read('config.lambda.ini')
    test_mode = config.get('run_mode', 'test')
    bots_config_location = '/tmp/bots.json'
    with open('/tmp/bots.json', 'wb') as f:
        boto3.client('s3').download_fileobj('3commas-compounder-data-bucket', 'bots.json', f)
    local = 'False'



def parameter_dict_getter(_secret_parameters_result):
    '''
    get the parameters from the System Manager and create a dict for easy access
    :param _secret_parameters_result: boto get_parameters result object
    :return: secrets_dict with last value after the slash as key
    '''
    constructed_secrets_dict = {}
    for params in _secret_parameters_result["Parameters"]:
        value_key = params["Name"].replace("/3commas-compounder/", "")
        constructed_secrets_dict[value_key] = params["Value"]
    return constructed_secrets_dict


# Check if local or AWS
if local == 'True':
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
        "/3commas-compounder/webhook_url",
    ]

    secret_parameters_result = boto_client.get_parameters(
        Names=secret_parameters,
        WithDecryption=True,
    )
    secrets_dict = parameter_dict_getter(secret_parameters_result)


if len(logging.getLogger().handlers) > 0:
    # The Lambda environment pre-configures a handler logging to stderr. If a handler is already configured,
    # `.basicConfig` does not execute. Thus we set the level directly.
    logging.getLogger().setLevel(logging.INFO)
else:
    logger = logging.getLogger(__name__)
    # Write to logfile
    logging.basicConfig(level=logging.INFO,
                        filename='logs/3commas_compounder.log',
                        filemode='a',
                        format='%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s',
                        datefmt='%d-%m-%Y %H:%M:%S'
                        )
    # Also print in console
    logging.getLogger().addHandler(logging.StreamHandler())



# connect 3commas python wrapper
py3cw_requst_options = {'request_timeout': 10, 'nr_of_retries': 1, 'retry_status_codes': [502]}
p3cw = Py3CW(key=secrets_dict["3commas_key"], secret=secrets_dict["3commas_secret"],
             request_options=py3cw_requst_options)

# Get telegram API ID + HASH from telegram API Development Tools
# tg_api_id = str(config.get('telegram', 'id'))
# tg_api_hash = str(config.get('telegram', 'hash'))


def refresh_balances(account_id):
    '''
    Refresh the balance 3c has for the given exchange
    :param account_id: id of exchange account on 3c
    :return:
    '''
    error, account_balances = p3cw.request(
            entity='accounts',
            action='load_balances',
            action_id=f'{account_id}',
        )

    if error:
        notify_webhook(error, 'ERROR')


def get_3c_currency_limit(bot_json):
    '''
    Helper function to get minimum BO amount for provided pair for DCA bot on 3c
    :param pair: pair we are looking up
    :return: tuple with (min BO, min price step size). Uses lotStep instead of priceStep to ensure we are using multiples the coin can also be sold at
    '''

    error, pair_limits = p3cw.request(entity='accounts',
                                      action='currency_rates',
                                      action_id='',
                                      payload={"market_code": bot_json['market_code'],
                                               "pair": bot_json['pairs'][0]}
                                      )

    if error:
        notify_webhook(error, 'ERROR')

    # This 3c Endpoint will not work when using short bots because those pairs (i.e ETH_USD) may not exist on the exchange
    # Workaround to start very small.
    # TODO: Check if this is working for short bots. Could potentially lead to a sell amount that is not allowed on the exchange since we dont have lotStep to reference
    # If so, may need to disable short bots from this script
    coins = bot_json['pairs'][0].split('_')
    if bot_json['currency'] != coins[0]:
        return (.001, .0001)

    # return (float(pair_limits['minTotal']), float(pair_limits['lotStep']))
    return (float(pair_limits['minTotal']), float(pair_limits['priceStep']))


def notify_webhook(message, message_type):
    '''
    Helper function to send error messages to telegram
    :param message:
    :return:
    '''
    if message_type == 'INFO':
        logging.info(message)
    if message_type == 'ERROR':
        logging.error(message)
    if message_type == 'WARNING':
        logging.warning(message)

    if "webhook_url" not in secrets_dict:
        return

    message_title_emoji = '📣' if message_type == 'INFO' else '⛔' if message_type == "ERROR" else '⚠️'
    message_embed_color = 3447003 if message_type == 'INFO' else 15158332 if message_type == "ERROR" else 16776960
    maintenece_icon = " - 🔧👷‍♂️🚧🏗️" if test_mode == "True" else " - ⚖️"
    discord_message = {
        'embeds': [
            {
                "title": f'{message_title_emoji} Compounder {message_type}{maintenece_icon}',
                "color": message_embed_color,
                "description": message
            }
        ]
    }
    resp = requests.post(secrets_dict["webhook_url"], json=discord_message)
    print(resp)


def update_bot(bot_id, valid_bo, valid_so, valid_mad, bot_json):
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
        logging.info("Test Run Completed!")
    else:
        error, update_bot = p3cw.request(
            entity='bots',
            action='update',
            action_id=f'{bot_id}',
            payload={
                'name': bot_json['name'],
                'pairs': bot_json['pairs'],
                'base_order_volume': f'{valid_bo}',  # this is auto calculated value that we're changing
                'take_profit': bot_json['tp'],
                'safety_order_volume': f'{valid_so}',  # this is auto calculated value that we're changing
                'martingale_volume_coefficient': bot_json['os'],
                'martingale_step_coefficient': bot_json['ss'],
                'max_safety_orders': bot_json['mstc'],
                'active_safety_orders_count': bot_json['active_safety_orders_count'],
                'safety_order_step_percentage': bot_json['sos'],
                'take_profit_type': bot_json['take_profit_type'],
                'strategy_list': bot_json['strategy_list'],
                'bot_id': bot_id,
                'max_active_deals': valid_mad,
            }
        )
        if error == {}:
            logging.info(f"{bot_json['name']} Updated!")
            logging.debug(f"{update_bot}")
        else:
            notify_webhook(f"{bot_json['name']} NOT completed:\n{error['msg']}", 'ERROR')


def calc_max_funds_per_deal(bo: float, so: float, mstc: int, sos: float, os: float, ss: float) -> float:
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


def get_config():
    '''
    Pulls necessary information from 3c api to generate config files
    :return:
    '''
    config_dict = {"accounts": {}}
    logging.debug('Pulling bot info...')
    ## Get list of all enabled bots to find out which accounts/currencies are needed to be optimized
    
    # infinite bots while loop
    bot_offset = 0
    bot_limit = 100
    keep_fetching_bots = True
    while keep_fetching_bots:
        bot_fetch_payload = {
            "scope": "enabled",
            "limit": f"{bot_limit}",
            "offset":f"{bot_offset}"
        }
        error, bots = p3cw.request(entity='bots', action='', payload=bot_fetch_payload)

        if error:
            notify_webhook(error, 'ERROR')

        # No more bots or less than needed for an additional loop.
        if len(bots) == 0 or len(bots) < 100:
            keep_fetching_bots = False
        else:
            # Potentially more bots avaialable.
            bot_offset += bot_limit

        for bot in bots:
            # Only support long bots that buy in quote and short bots that sell in base
            if (bot['strategy'] == 'long' and (bot['base_order_volume_type'] != 'quote_currency' or bot[
                'safety_order_volume_type'] != 'quote_currency')) or (bot['strategy'] == 'short' and (
                    bot['base_order_volume_type'] != 'base_currency' or bot[
                'safety_order_volume_type'] != 'base_currency')):
                logging.warning(f'Only Long Quote and Short Base bots are supported. Skipping {bot["name"]}')
                continue

            # If account not already in config_dict, add it
            account_id = bot['account_id']
            
            if account_id not in config_dict['accounts']:
                config_dict['accounts'][account_id] = {}
                config_dict['accounts'][account_id]['account_name'] = bot['account_name']
                config_dict['accounts'][account_id]['balances'] = {}
                config_dict['accounts'][account_id]['bots'] = {}
            
            bot_id = bot['id']

            config_dict['accounts'][account_id]['bots'][bot_id] = {}
            
            # readibility short-cut
            bot_config_dict = config_dict['accounts'][account_id]['bots'][bot_id]
            # Add 3c settings
            bot_config_dict['name'] = bot['name']
            bot_config_dict['bo'] = bot['base_order_volume']
            bot_config_dict['so'] = bot['safety_order_volume']
            bot_config_dict['os'] = bot['martingale_volume_coefficient']
            bot_config_dict['ss'] = bot['martingale_step_coefficient']
            bot_config_dict['mad'] = bot['max_active_deals']
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
            config_dict['accounts'][account_id]['bots'][bot_id]['currency'] = currency
            # If currency not already in the account, add it
            config_dict['accounts'][account_id]['balances'][currency] = 0.0
            # Add market code so we can look up currency limits for that exchange later
            error, account_info = p3cw.request(entity='accounts', action='account_info', action_id=str(account_id))
            if error:
                notify_webhook(f'Error getting account info for [{bot["account_name"]}](https://3commas.io/accounts/{bot["account_id"]})', 'ERROR')

            config_dict['accounts'][bot['account_id']]['bots'][bot['id']]['market_code'] = account_info['market_code']
            
    logging.debug('Pulling account balances...')
    # Get balance for every currency for each account
    for account_id in config_dict['accounts'].keys():
        # Refresh the balance 3c has for the exchange
        refresh_balances(account_id)
        error, account_balances = p3cw.request(entity='accounts', action='account_table_data', action_id=str(account_id), payload={"account_id": account_id})

        if error:
            print(error)
            notify_webhook(f'Error getting account table data for [{account_id}](https://3commas.io/accounts/{account_id})', 'ERROR')

        # Update balances we have bots using for the given account
        for pair in account_balances:
            if pair['currency_code'] in config_dict['accounts'][account_id]['balances']:
                config_dict['accounts'][account_id]['balances'][pair['currency_code']] += float(pair['equity'])


    # Add in deal balances for bots that can get compounded
    for account_id in config_dict['accounts'].keys():
        for bot_id in config_dict['accounts'][account_id]['bots'].keys():

            error, active_deals = p3cw.request(
                entity='deals',
                action='',
                payload={
                    "account_id": account_id,
                    "scope": "active",
                    "bot_id": bot_id
                    }
                )
            if error:
                print(error)
                notify_webhook(f'Error getting active deals for [{account_id}](https://3commas.io/accounts/{account_id})', 'ERROR')

            for deal in active_deals:
                currency_code = get_currency(deal['pair'], deal['strategy'], deal['base_order_volume_type'])
                if currency_code in config_dict['accounts'][account_id]['balances']:
                    config_dict['accounts'][account_id]['balances'][currency_code] += float(deal['bought_volume' if deal['strategy'] == 'long' else 'sold_amount'])

    # DO NOT GRAB ALLL ACTIVE DEALS YOU WILL OVER EXTEND
    # Add in deal balances to actual balances to get total funds in account
    # for account_id in config_dict['accounts'].keys():
    #     error, active_deals = p3cw.request(entity='deals', action='',
    #                                     payload={"account_id": account_id, "scope": "active"})

    #     for deal in active_deals:
    #         # Get the currency the deal is getting 'bought' with
    #         currency_code = get_currency(deal['pair'], deal['strategy'], deal['base_order_volume_type'])
    #         # Add this amount to the balance already in the dict
    #         if currency_code in config_dict['accounts'][account_id]['balances']:
    #             config_dict['accounts'][account_id]['balances'][currency_code] += float(deal['bought_volume' if deal['strategy'] == 'long' else 'sold_amount'])
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
            bot_ids = [k for k in user_conf['accounts'][account_id]['currencies'][currency].keys()]
            if len(bot_ids) == 1:
                user_conf['accounts'][account_id]['currencies'][currency][bot_ids[0]]['allocation'] = 1.0

    # Write config to file
    with open(bots_config_location, "w") as outfile:
        json.dump(user_conf, outfile, indent=4)


def check_user_config(config):
    '''
    Checks that the bots.json file is configured and has all necessary data for script to run
    :return: valid user config
    '''

    # Check if the bots.json file exists, if not create it and prompt user
    file_exists = exists(bots_config_location)

    if not file_exists:
        if local == 'True':
            create_user_config(config)
            notify_webhook(f'Could not find a `bots.json` in {bots_config_location}, please populate the newly created file.', 'ERROR')
        else:
            notify_webhook(f'Could not find a `bots.json` in {bots_config_location}, please check s3 file download.', 'ERROR')
        return False
    # if bots.json file DOES exist, make sure it has all of the bot ids and all allocations are defined
    else:
        with open(bots_config_location, "r") as infile:
            user_config = json.load(infile)
            bot_ids = []
            for account_id in user_config['accounts']:
                account_name = user_config['accounts'][account_id]['account_name']
                for currency in user_config['accounts'][account_id]['currencies']:
                    allocation = 0
                    for bot_id in user_config['accounts'][account_id]['currencies'][currency]:
                        bot_ids.append(int(bot_id))
                        # If a bot doesn't have its allocation defined, break
                        bot_allocation = user_config['accounts'][account_id]['currencies'][currency][bot_id][
                            'allocation']
                        bot_name = user_config['accounts'][account_id]['currencies'][currency][bot_id]['bot_name']
                        if not bot_allocation:
                            notify_webhook(f'{account_name} "{bot_name}" does not have an allocation defined', 'ERROR')
                            return False
                        allocation += float(bot_allocation)
                    # Throw warning if risk is greater than 100%
                    if allocation > 1:
                        logging.warning(f'{account_name} {currency} has risk factor of {allocation * 100}')
            # if there is a live bot that does not have a config in bots.json, break
            for account_id in config['accounts']:
                for bot_id in config['accounts'][account_id]['bots']:
                    if bot_id not in bot_ids:
                        notify_webhook(
                            'bots.json is missing new bots. Please delete the file and re-run this script', 'ERROR')
                        return False
        return user_config


def optimize_bot(bot_id, bot_json, max_currency_allocated, bot_max_active_deals):
    '''
    Helper function to find optimal bot settings and update 3c via api
    :param bot_id: 3c ID of the bot
    :param bot_json: bot settings to give to calc_max_funds_per_deal
    :param max_currency_allocated: total amount of funds we are allocating to the bot
    :return:
    '''
    logging.info(f'max_currency_allocated: {max_currency_allocated}')


    # Get min BO and price step for currency on given exchange
    currency_limits = get_3c_currency_limit(bot_json)
    bo = currency_limits[0]
    # Get ratio of BO:SO from bot settings
    boso_ratio = float(bot_json['so']) / float(bot_json['bo'])
    # Maintain ratio user has in their settings
    so = bo * boso_ratio
    # Get minimum max_funds for bot settings to use as starting point
    mstc = int(bot_json['mstc']) # Max Safety Trades Count
    sos = float(bot_json['sos']) # Safety Order Scale
    os = float(bot_json['os']) # Order Scale
    ss = float(bot_json['ss']) # Safety Scale
    max_funds_per_deal = calc_max_funds_per_deal(bo=bo, so=so, mstc=mstc, sos=sos, os=os, ss=ss)

    valid_bo = bo
    valid_so = so
    valid_mad = 1

    ### 09-02-2022 SanCoca BO SO MAD optimiser
    bot_type = bot_json["type"]
    bot_name = bot_json["name"]
    potential_max_deals = max_currency_allocated / max_funds_per_deal
    floor_max_deals = math.floor(potential_max_deals)
    logging.info(f'bot_type: {bot_type}')
    logging.info(f'floor_max_deals: {floor_max_deals}')
    
    # Make sure the bot can actually use 1 or more deals
    if floor_max_deals >= 1:
        if bot_type == "Bot::MultiBot":
            if potential_max_deals > bot_max_active_deals:
                # Potential max deals is greater than we want it to be (6), we need to increase BO:SO scale based on residual max deals
                # max_active_deals = 6
                # potential_max_deals = 8.6
                # remainig_deal_space = potential_max_deals - max_active_deals = 2.6
                # BO:SO scale increase = remaining_deal_space / max_active_deals = 0.433

                remainig_deal_space = potential_max_deals - bot_max_active_deals
                logging.info('remainig_deal_space', remainig_deal_space)

                deal_bo_so_increase = remainig_deal_space / bot_max_active_deals
                logging.info('deal_bo_so_increase', deal_bo_so_increase)

                valid_bo += bo * deal_bo_so_increase
                valid_so += so * deal_bo_so_increase
                valid_mad = bot_max_active_deals
            else: 
                # we are still under max active deals (6) so we need to scale based on what is left over 
                # potential_max_deals = 3.8
                # max_deals = math.floor(potential_max_deals) = 3
                # remainig_deal_space = potential_max_deals - max_deals = 0.8
                # BO:SO scale increase = remaining_deal_space / max_deals = 0.26
                remainig_deal_space = potential_max_deals - floor_max_deals
                logging.info('remainig_deal_space', remainig_deal_space)

                deal_bo_so_increase = remainig_deal_space / floor_max_deals
                logging.info('deal_bo_so_increase', deal_bo_so_increase)

                valid_bo += bo * deal_bo_so_increase
                valid_so += so * deal_bo_so_increase
                valid_mad = floor_max_deals
        elif bot_type == "Bot::SingleBot":
            valid_bo = bo * potential_max_deals
            valid_so = so * potential_max_deals
    else: 
        notify_webhook(f'Not enough funds for 1 active deal, using minimum bo:so for bot: [{bot_name}](https://3commas.io/bots/{bot_id})', 'WARNING')


    max_funds_per_deal_new_size = calc_max_funds_per_deal(bo=valid_bo, so=valid_so, mstc=mstc, sos=sos, os=os, ss=ss)
    logging.info(f'max_funds_per_deal_new_size: {max_funds_per_deal_new_size}')

    total_funds_used_by_bot = max_funds_per_deal_new_size * valid_mad
    logging.info(f'total_funds_used_by_bot: {total_funds_used_by_bot}')


    ### OLD HebrewHammer:
    # # calc_max_funds_per_deal is per deal, so divide max_currency_allocated by # deals (MAD)
    # per_deal_allocation = max_currency_allocated / int(bot_json['mad'])
    # logging.info(f'Optimizing for {bot_json["name"]} ({bot_id}) with max budget {max_currency_allocated} ({per_deal_allocation} per deal)')

    # print('BOT TYPE:', bot_json['type'])
    # # If not enough funds before optimizing, throw warning
    # if max_funds_per_deal > per_deal_allocation:
    #     notify_webhook(f'Not enough funds for minimal settings for {bot_json["name"]}', 'WARNING')
    # else:
    #     while max_funds_per_deal <= per_deal_allocation:
    #         bo += currency_limits[1]
    #         so = float(bo * boso_ratio)
    #         max_funds_per_deal = calc_max_funds_per_deal(bo=bo, so=so, mstc=int(bot_json['mstc']), sos=float(bot_json['sos']),
    #                                    os=float(bot_json['os']), ss=float(bot_json['ss']))
    #         # Store working bo/so so we know what to give to api
    #         if max_funds_per_deal <= per_deal_allocation:
    #             valid_bo = bo
    #             valid_so = so

    logging.info(f'Optimal settings for {bot_json["name"]} ({bot_id}) found! BO: {round(valid_bo, 8)}, SO: {round(valid_so, 8)}, MAD: {valid_mad}')

    # Send api requests to 3c to update the bot
    update_bot(bot_id, valid_bo, valid_so, valid_mad, bot_json)

def compounder_start():
    # Get bot configs from 3c
    config = get_config()
    # Compare 3c live configs against user config (bots.json)
    user_config = check_user_config(config)
    # If configs are good, update bots
    if user_config:
        logging.info('Valid config found, proceeding to update bots...')
        # Update the bots
        for account_id in config['accounts']:
            # Get account balances
            account_balances = config['accounts'][account_id]['balances']
            # Loop through each bot to multiply the allocation against total balance for the accounts currency balance
            for bot_id in config['accounts'][account_id]['bots']:
                bot_json = config['accounts'][account_id]['bots'][bot_id]
                bot_currency = bot_json['currency']

                # Print account balance
                logging.info(f"{config['accounts'][account_id]['account_name']} {bot_currency} balance: {account_balances[bot_currency]}")

                # Get the allocation for the bot from user_config
                user_conf_bot = user_config['accounts'][f'{account_id}']['currencies'][bot_currency][f'{bot_id}']

                bot_allocation = user_conf_bot['allocation']
                # check if bot has Max active deal, if it does use it otherwise set it to 1
                bot_max_active_deals = user_conf_bot['max_active_deals'] if "max_active_deals" in user_conf_bot else 1
                
                max_currency_allocated = float(account_balances[bot_currency]) * float(bot_allocation)
                logging.info(f"Allocation allowed: {max_currency_allocated} {bot_currency}")

                # Pass the settings to optimize function to find optimal BO:SO for allocation
                optimize_bot(bot_id=bot_id, bot_json=bot_json, max_currency_allocated=max_currency_allocated, bot_max_active_deals=bot_max_active_deals)
#

def request_handler(event, lambda_context):
   compounder_start()


if __name__ == "__main__":

    # Startup telegram
    # if not exists('/telegram/name.session'):
    #     notify_webhook('TG Configured!', 'INFO')
   compounder_start()

