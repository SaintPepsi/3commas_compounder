"""
Handle discord webhooks
"""

import configparser

from genericpath import exists
import requests
import logger
import utils


LOCAL = exists('config.ini')

if LOCAL:
    # Parse/Read config.ini we're locally running this
    config = configparser.ConfigParser()
    config.read('config.ini')
    webhook_url = config.get('discord', 'webhook_url')
else:
    # Get parameter from AWS parameter store
    secret_parameters = [
        "/3commas-compounder/webhook_url",
    ]
    webhook_url = utils.get_param_dict_from_ssm(secret_parameters)['webhook_url']


MESSAGE_TYPE_EMOJI = {
    'INFO': '📣',
    'WARNING': '⚠️',
    'ERROR': '⛔'
}

MESSAGE_TYPE_COLOR = {
    'INFO': 3447003,
    'WARNING': 16776960,
    'ERROR': 15158332
}

TEST_MODE_MESSAGE_DICT = {
    "False": " - ⚖️",
    "True": " - 🔧👷‍♂️🚧🏗️"
}


def notify_webhook(message: str, message_type: str):
    '''
    Helper function to send error messages to telegram
    :param message: message to send to webhook
    :param message_type: INFO, WARNING, ERROR
    :param real_signal: boolean
    :return:
    '''

    # log a message based on warning level
    logger.log(message=message, message_type=message_type)

    discord_message = {
        'embeds': [
            {
                "title": (
                    f'{MESSAGE_TYPE_EMOJI[message_type]} Compounder '
                    f'{message_type}{TEST_MODE_MESSAGE_DICT[str(LOCAL)]}'
                ),
                "color": MESSAGE_TYPE_COLOR[message_type],
                "description": message
            }
        ]
    }

    resp = requests.post(webhook_url, json=discord_message)
    logger.log(resp, "INFO")
