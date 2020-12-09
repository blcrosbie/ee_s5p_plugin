import os
import sys
import json
from google.oauth2.credentials import Credentials
import ee

def ee_init():
    try:
        ee.Initialize()
    except Exception as e:
        print(e)
        ee.Authenticate()
        ee.Initialize() 


if __name__ == '__main__':
    ee_init()

# # find directory for file management and get config
# CURRENT_DIR = os.getcwd()

# # parse config file for secure variables needed for functions
# CONFIG_DIR = os.path.join(CURRENT_DIR, ".config")
# config_file = os.path.join(CONFIG_DIR, "config.json")

# # load config
# with open(config_file, 'r') as cfg:
#     config = json.load(cfg)

# ee_api_key = config["GOOGLE"]["EARTH_ENGINE_REFRESH_TOKEN"] # 'api key for earth engine'
# auth_token = ee.Authenticate()
# print(auth_token)
