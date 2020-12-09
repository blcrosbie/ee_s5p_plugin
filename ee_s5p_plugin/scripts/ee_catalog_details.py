
import os, sys, json

import numpy as np

#import pandas as pd
#import geopandas as gpd
#import shapely
#from shapely.geometry import LinearRing, Polygon
#import pyproj

import datetime

#import geemap
#from ipyleaflet import GeoJSON

import ee
from setup_gee import ee_init
ee_init()



def footprint2geometry(fp):
    coordinates = fp['coordinates']
    ring = LinearRing(coordinates)
    s = Polygon(ring)
    return s

def translate_unix_time(Unix_Time_ms):
    # our unix time stamp in ms, change back to s
    seconds = Unix_Time_ms/1000
    translate_date = datetime.datetime.utcfromtimestamp(seconds).strftime("%Y-%m-%d")
    return translate_date


    
# def get_name_options(name):
    
#     # first name should be all '/'
#     # go from back to front replacing / with _
#     all_name_options = [name]
#     options_count = len(name.split('_'))

#     print(options_count)
    
#     if options_count-1 > 1:
#         for i in range(options_count-1):
#             # first option all replaced already in list
#             first = name.split('_')[0:options_count-i-1]
#             last = name.split('_')[options_count-i-1: options_count]
#             adjusted_name = '/'.join(first) + '_' + '_'.join(last)
#             all_name_options.append(adjusted_name)

#     last_option = name.replace('_', '/')

#     if last_option not in all_name_options:
#         all_name_options.append(last_option)

        
#     # find the right name that will satisfy the ImageCollection argument
#     return all_name_options

def get_ImageCollection_info(IC_id):
    # from an image collection:
    # 1. extract the list of images included in this set
    
    # all_id_options = get_name_options(name)
    # for IC_id in all_id_options:

    image_collection = ee.ImageCollection(IC_id)
    features = image_collection.getInfo()['features']
    df = pd.DataFrame(dtype=object)

    row_count = 0

    for feat in features:
        feat_id = feat['id']
        start = feat['properties']['system:time_start']
        start_date = translate_unix_time(start)
        end = feat['properties']['system:time_end']
        end_date = translate_unix_time(end)
        footprint = feat['properties']['system:footprint']
        geometry = footprint2geometry(footprint)

        for band in feat['bands']:
            # bands.append(band['id'])
            band_crs = band['crs']
            band_dim_x = band['dimensions'][0]
            band_dim_y = band['dimensions'][1]
            this_feat = {'id': feat_id, 'band': band['id'], 'crs':band_crs, 'dim_x': band_dim_x, 'dim_y': band_dim_y, 'start':start, 'end':end, 'start_date': start_date, 'end_date': end_date, 'geometry': geometry}
            this_df = pd.DataFrame(this_feat, index=[row_count], dtype=object)
            row_count += 1
            df = pd.concat([df, this_df], axis=0)



    return df


def get_Image_info(img_id):
    image = ee.Image(img_id)
    features = image.getInfo()
    # print(features)

    return


def get_FeatureCollection_info(FC_id):
    FC = ee.FeatureCollection(FC_id)
    features = FC.getInfo()
    print(features)
    print(STOP)
    return



def get_info(dataset):
    dataset_id = dataset['dataset_id']
    dataset_type = dataset['dataset_type']

    if dataset_type == 'ImageCollection':
        metadata = get_ImageCollection_info(dataset_id)
    elif dataset_type == 'Image':
        metadata = get_Image_info(dataset_id)
    elif dataset_type == 'FeatureCollection':
        metadata = get_FeatureCollection_info(dataset_id)
    else:
        print("Undefined Type: ", dataset_type)

    return metadata


    # 2. for each image, extract the geography (polygon), time frame, and bands (info)
    #  create shapefile to autoload into the GUI for these filters
    # 2a. Polygon
    # 2b. Time Frame
    # 2c. Band (and get Band info for specific data searches)



    # 3. for the whole collection, extract the provider/source of data for citation
    # 3a. in 'properties' -> 'provider', 'provider_url', 'source_tags'
    

            # 1b. with ImageCollection Name, we can run ee.ImageCollection(<name>).getInfo()
        #    to extract geometry data and other properties for metadata filtering
        # IC_details = get_ImageCollection_info(IC)



####################################################################

def get_ee_catalog():
    catalog_fn = os.path.join('metadata', 'gee_catalog.json')
    fpath = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fn = os.path.join(fpath, catalog_fn)

    if os.path.exists(fn):
        with open(fn, 'r') as in_file:
            data = json.load(in_file)
            return data

    else:
        print("No Catalog Found...\nnow running ee_catalog.py")
        import ee_catalog
        ee_catalog.get_catalog()
        return get_ee_catalog()


####################################################################

# "dataset_id" "dataset_type"
def analyze_catalog():
    # self.get_ee_catalog()
    catalog = get_ee_catalog()
    print("Number of Datasets in Catalog:", len(catalog))

    dataset_types = []
    # dataset_ids = []
    type_id_lookup = {}
    all_tags = []
    tag_id_lookup = {}

    for dataset in catalog:
        d_type = dataset['dataset_type']
        d_id = dataset['dataset_id']
        d_tags = dataset['tags']
        # print(d_type)
        # print(d_id)
        # print(d_tags)

        if d_type not in dataset_types:
            dataset_types.append(d_type)
            type_id_lookup.update({d_type: [d_id]})

        else:
            type_id_lookup[d_type].append(d_id)


        for tag in d_tags:
            if tag not in all_tags:
                all_tags.append(tag)
                tag_id_lookup.update({tag: [d_id]})
            else:
                tag_id_lookup[tag].append(d_id)



    print(dataset_types)
    print(type_id_lookup)
    print(all_tags)
    # print(tag_id_lookup)


def test_get_info():
    final_df = pd.DataFrame(dtype=object)

    for dataset in catalog:       
        # # for comprehensive tag list
        # for tag in dataset['tags']:
        #     if tag not in all_tags:
        #         all_tags.append(tag)

        try:
            dataset_df = get_info(dataset)
        except Exception as e:
            print("Failed on:", dataset)
            print(e)
            dataset_df = pd.DataFrame()



        if dataset_df is not None:
            final_df = pd.concat([final_df, dataset_df], axis=0)

    gdf = gpd.GeoDataFrame(final_df, geometry=final_df.geometry)
    fn = 'gee_catalog.geojson'
    outfile = os.path.join(os.getcwd(), os.path.join('catalog', fn))
    gdf.to_file(outfile, driver="GeoJSON")



def test_ee_catalog_details():
    analyze_catalog()
    





if __name__ == '__main__':
    test_ee_catalog_details()




