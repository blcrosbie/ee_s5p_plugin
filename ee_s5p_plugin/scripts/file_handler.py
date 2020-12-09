import os, sys
import csv
import json
import geojson





def save_csv(data, fileName):
    """ data: list of dictionaries
        fileName: filePath//fileName.fileType"""
    with open(fn, 'w', newline='') as fw:
        csvwriter = csv.writer(fw)
        csvwriter.writerow(data[0].keys())
        for rw in save_data:
            csvwriter.writerow(rw.values())

def save_json(data, fileName):
    """ data: list of dictionaries (already a json object)
        fileName: filePath//fileName.fileType"""
    with open(fn, 'w') as fw:
        json.dump(data, fw, indent=4)


def save_geojson(data, fileName):
    """ data: list of dictionaries
    fileName: filePath//fileName.fileType"""

    # if data is a list of dictionaries, we need to reformat
    # into geojson data, using any columns in the file that look like Lat/Lon pairs

    # if data is a dictionary, with first term: {'type': "FeatureCollection"}
    # save directly to geojson, no restructuring necessary
    
    with open(fn, 'w') as fw:
        geojson.dump(data, fw, indent=4)


