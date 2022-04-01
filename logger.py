"""
Log messages based on severity
"""

import logging

if len(logging.getLogger().handlers) > 0:
    # The Lambda environment pre-configures a handler logging to stderr.
    # If a handler is already configured,
    # `.basicConfig` does not execute. Thus we set the level directly.
    logging.getLogger().setLevel(logging.INFO)
else:
    logger = logging.getLogger(__name__)
    # Write to logfile
    logging.basicConfig(level=logging.INFO,
                        filename='logs/3commas_compounder.log',
                        filemode='a',
                        format=(
                            '%(asctime)s.%(msecs)03d '
                            '%(levelname)s %(module)s - '
                            '%(funcName)s: %(message)s'
                        ),
                        datefmt='%d-%m-%Y %H:%M:%S'
                        )
    # Also print in console
    logging.getLogger().addHandler(logging.StreamHandler())

MESSAGE_TYPE_LOGGING = {
    'INFO': logging.info,
    'WARNING': logging.warning,
    'ERROR': logging.error
}

def log(message: str, message_type: str):
    '''
    Logs the message to file according to type
    '''
    MESSAGE_TYPE_LOGGING[message_type](message)
