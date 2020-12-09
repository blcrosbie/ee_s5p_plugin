import os, sys
import json
import datetime

import requests
from bs4 import BeautifulSoup

def get_td_name(td):
    a_tag = td.findAll('a')
    for a in a_tag:
        if 'href' in a.attrs:
            name = a.attrs['href'].split('/')[-1]
            # td_name = name.replace('_', '/')
            return name
        else:
            print(a)   
    
    return None

    
# def get_ImageCollection(name):
    
#     all_name_options = [name.replace('_', '/')]
#     options_count = len(name.split('_'))
    
#     if options_count-1 > 1:
#         for i in range(options_count):
#             # first option all replaced already in list
#             split_name = name.split('_')

#     else:
#         # add in the original text, with underscore
#         all_name_options.append(name)

        
#     # find the right name that will satisfy the ImageCollection argument
#     for id_option in all_name_options:
    
#         try:
#             IC = ee.ImageCollection(id_option)
#             if IC:
#                 return id_option, IC

#         except Exception as e:
#             print(e)
#             print("NEED TO ADJUST NAME: ", name, all_name_options)
            
    

def get_ImageCollection_tags(td):
    tag_list = []
    a_tag = td.findAll('a')
    for a in a_tag:
        if 'href' in a.attrs:
            tag = a.attrs['href'].split('/')[-1]
            if tag not in tag_list:
                tag_list.append(tag)
            
    return tag_list


def get_tbody_info(tbody):
    tbody_info = {}
    td_list = tbody.findAll('td')

    for td in td_list:
        if td.attrs['class'] == ['ee-dataset']:
            td_name = get_td_name(td)
            
        elif td.attrs['class'] == ['ee-dataset-description-snippet']:
            quick_description = td.text
            
        elif td.attrs['class'] == ['ee-tag-buttons', 'ee-small']:
            tag_list = get_ImageCollection_tags(td)
            
        else:
            print("Undefined attributes in td object: ", td.attrs)
        

    # IC_id, image_collection = get_ImageCollection(td_name)
        
    tbody_info = {'dataset': td_name,
                   'tags': tag_list,
                   'description': quick_description
                  }

    return tbody_info


def parse_code_block(code_block):
    """ Break out the Code Embedded on Earth-Engine Dataset URL

    Example output of code_block:

        ee.ImageCollection("<dataset_id>")

    dataset_type follows the ee.-> before first '('
    dataset_id inside " "

    """
    # Extract Datatepy in code block
    dataset_type = code_block.text.split('.')[1]
    dataset_type = dataset_type.split('(')[0]

    # Extract ID from the code block 'ee.Image...("<ID>")'
    dataset_id = code_block.text.split('(')[1]
    dataset_id = dataset_id.split(')')[0]
    dataset_id = dataset_id.replace('"','')

    return dataset_type, dataset_id



def validate_availability(text):
    """ we will use this a test function to extract a start/end date"""

    # Test 0: ensure the text is actually a string before splitting
    try:
        assert isinstance(text, str), "not a string"
    except Exception as e:
        # raise Exception('Date Validate Failed: {}'.format(e))
        print(e)
        return None

    # Test 1: The text must be split evenly by ' - '
    split_text = text.split(' - ')
    try:
        assert len(split_text) == 2, "len()==2 Test"
    except Exception as e:
        # raise Exception('Date Validate Failed: {}'.format(e))
        print(e)
        return None

    # Test 2: DateTime Formatting
    # Test 2a: First split has DateTime format (Most Common ISO format)
    try:
        # date_start = datetime.strptime(split_text[0], '%Y-%m-%')
        date_start = datetime.datetime.fromisoformat(split_text[0])

    # Test 2b: Use custom strptime, non-ISO
    except Exception as e:
        date_start = datetime.datetime.strptime(split_text[0], "%Y-%m-%dT%H:%M:%S.%fZ")
        assert isinstance(date_start, datetime.datetime), "Not a Datetime object"

    except:
        # raise Exception('Date Validate Failed: {}'.format(e))
        print("Not a datetime object: {}".format(split_text[0]))
        return None

    finally:
        # Since we are packing this into a json file, the best way is to keep string
        # the date end will be determined later, but if we succeeded in date start
        # We have found our Date Availability Match!
        date_range = {'dataset_start': split_text[0], 'dataset_end': split_text[1]}
        return date_range


def seek_date_availability(soup):
    """ This is difficult to be exact, the html is not clearly/uniquely marked"""
    date_range = None
    # Method 1: the first dd on the page:
    try:
        dd = soup.find("dd")
        date_range = validate_availability(dd.text)
        assert date_range is not None, "Method 1"
        return date_range
    except Exception as e:
        # raise Exception('Failed on {}, {}'.format(dd, e))
        print(e)

    # Method 2: all dd's on page, find the one that satisfies a time format validation
    for dd in soup.findAll("dd"):
        date_range = validate_availability(dd.text)
        if date_range is not None:
            return date_range

    print("METHOD 2 FAILED TO FIND DATE AVAILABILITY in each Method, SET DEFAULT")
    # determine Earliest and Latest Date and set those as defaults
    default_start = None
    default_end = None
    date_range = {'dataset_start': default_start, 'dataset_end': default_end}
    return date_range




def follow_dataset_link(tag_name, URL):
    dataset_url = URL + '/' + tag_name
    response = requests.get(dataset_url)
    htmlCode = response.text
    soup = BeautifulSoup(response.text, 'html.parser')
    code = soup.find("code")

    dataset_type, dataset_id = parse_code_block(code)
    result = {'dataset_id': dataset_id, 'dataset_type': dataset_type}
    result.update(seek_date_availability(soup))

    return result


def save_catalog(results, partial=False):
    """Save this simple metadata to json """
    PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    METADATA_DIR = os.path.join(PLUGIN_DIR, "metadata") 

    # Base Filename
    fn = 'gee_catalog'
    ftype = '.json'
    
    # if we have partial results, track the last result index
    if partial:
        fn = fn + '_(' + str(len(results)-1) + ')'

    # Add all
    fn = fn + ftype
    # now get full file path + name
    filename = os.path.join(METADATA_DIR, fn)

    with open(filename, 'w') as outfile:
        json.dump(results, outfile, indent=4)



def get_catalog():
    #=====================================
    # 0. Run the requests on the Google Earth Engine Datasets Catalog
    #=====================================

    # From the "View all datasets" Tab on developers.google.com/earth-engine
    url = "https://developers.google.com/earth-engine/datasets/catalog"
    response = requests.get(url) 
    htmlCode = response.text 
    soup = BeautifulSoup(response.text, 'html.parser') 

    # the 'tbody' element seems to be the best way to extract the useful metadata for each dataset
    all_tbodies = soup.findAll("tbody")

    # total GEE datasets: 409 as of 2020-07-21
    # print(len(all_tbodies))

    #=====================================
    # 1. Iterate through each tbody to extract metadata
    #=====================================

    # this will result in a list of each dataset saved as a json metadata file
    # for allowing user to filter on tags/geography/time before 
    # making requests to the gee server
    webscrape_results = []

    for tbody in all_tbodies:
        # 1a. tbody will have 3 basic infos: ImageCollection Name, Tags, Description
        
        try:
            this_result = get_tbody_info(tbody)
            more_result = follow_dataset_link(this_result['dataset'], url)
            this_result.update(more_result)
            webscrape_results.append(this_result)

            update_msg = "Webscrape Status: {} out of {}".format(len(webscrape_results), len(all_tbodies))
            print(update_msg, end="\r", flush=True)

        except Exception as e:
            print(e)
            print("failed data extract on: ", tbody)
            save_catalog(webscrape_results, partial=True)
            return


    #=====================================
    # 2. Save All metadata to json, all tests passed
    #=====================================
    save_catalog(webscrape_results, partial=False)



def working_catalog():
    """ Manual filter for current working version"""
    all_catalog = read_catalog()
    working_IC_set = [
            'WorldPop/GP', 
            'S5P', 
            'NCEP_RE', 
            'NOAA'
            # 'NOAA/GOES', 
            # 'NOAA/NWS',
            # 'NOAA/VIIRS'
            ]
    non_working_IC_set = [
            'NOAA/CFSR'
            'NOAA/DMSP-OLS/NIGHTTIME_LIGHTS'
            ]

    non_working_FC_set = [
            'BLM/AIM/v1/TerrADat/TerrestrialAIM',
            'FAO/GAUL/2015/level0',
            'NOAA/NHC/HURDAT2/atlantic',
            'NOAA/NHC/HURDAT2/pacific'
            ]

    use_catalog = []

    for data in all_catalog:

        if data['dataset_type'] == 'FeatureCollection':
            if data['dataset_id'] not in non_working_FC_set:
                use_catalog.append(data)

        if data['dataset_type'] == 'ImageCollection':
            if data['dataset_id'] in non_working_IC_set:
                pass
            else:
                for working in working_IC_set:
                    if working in data['dataset_id']:
                        use_catalog.append(data)
        


    save_catalog(use_catalog)
    return


def read_catalog():
    catalog_fn = os.path.join('metadata', 'gee_catalog.json')
    fpath = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fn = os.path.join(fpath, catalog_fn)

    if os.path.exists(fn):
        with open(fn, 'r') as in_file:
            data = json.load(in_file)
            return data

    else:
        print("No Catalog Found...\nnow running ee_catalog.py")
        get_catalog()
        return read_catalog()



def test():
    working_catalog()

if __name__ == '__main__':
    read_catalog()
    # working_catalog()
