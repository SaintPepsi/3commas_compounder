'''
Some utils
'''
# AWS
import boto3

def parameter_dict_getter(_secret_parameters_result):
    '''
    get the parameters from the System Manager and create a dict for easy access
    :param _secret_parameters_result: boto get_parameters result object
    :return: secrets_dict with last value after the slash as key
    '''
    constructed_secrets_dict = {}
    for params in _secret_parameters_result["Parameters"]:
        value_key = params["Name"].rsplit("/", maxsplit=1)[1]
        constructed_secrets_dict[value_key] = params["Value"]
    return constructed_secrets_dict

def get_param_dict_from_ssm(parameters):
    '''
    Connects to ssm given the parameters to get
    '''
    boto_client = boto3.client("ssm")

    secret_parameters_result = boto_client.get_parameters(
        Names=parameters,
        WithDecryption=True,
    )
    secret_dict = parameter_dict_getter(secret_parameters_result)
    return secret_dict
