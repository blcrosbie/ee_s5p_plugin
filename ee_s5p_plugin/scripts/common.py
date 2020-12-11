import os, sys
import json
import math
import requests
import platform
import subprocess



#==============================================================
# Find Public IP Lat/Long
#==============================================================

def find_me():
    """ IP BASED LOCATION FINDER
    Requires BeautifulSoup4"""

    try:
        from bs4 import BeautifulSoup
    except:
        if platform.system() == 'Windows':
            subprocess.check_call(['python', '-m', 'pip', 'install', 'beautifulsoup4'])
            # subprocess.check_call(['python', '-m', 'pip', 'install', 'geojson'])
        else:
            try:
                os.system("pip install beautifulsoup4")
            except:
                os.system("sudo pip install beautifulsoup4")



    url = "https://iplocation.com"
    response = requests.get(url) 
    htmlCode = response.text 
    soup = BeautifulSoup(response.text, 'html.parser')
    result = soup.findAll('td')
    lat = 0
    lng = 0
    public_ip = ''
    city = ''
    region = ''
    country = ''

    for res in result:
        if res.find("b"):
            public_ip = res.text.split(' ')[0]
        elif 'class' in res.attrs:
            if res.attrs['class'] == ['lat']:
                lat = float(res.text)
            elif res.attrs['class'] == ['lng']:
                lng = float(res.text)
            elif res.attrs['class'] == ['city']:
                city = res.text.split(' ')[0]
            else:
                pass
        elif res.find("span"):
            span = res.find("span")
            if span.attrs['class'] == ['country_name']:
                country = span.text
            elif span.attrs['class'] == ['region_name']:
                region = span.text
            else:
                pass

        else:
            pass

    result = {'lat': lat, 'lon': lng, 'city': city, 'region': region, 'country': country}
    return result



#==============================================================
# Odd-Even Algorithm
#==============================================================

def is_point_in_path(x , y, poly):
    num = len(poly)
    j = num - 1
    c = False
    for i in list(range(0, num)):
        if (poly[i][1] > y) != (poly[j][1] > y):
            if (x < poly[i][0] + (poly[j][0] - poly[i][0]) * (y - poly[i][1])/(poly[j][1] - poly[i][1])):
                c = not c
        j = i
    return c

#==============================================================
# GeoFilter/ Polygon Building Functions
#==============================================================    

def convert_miles_to_km(distance):
    return distance*1.60934

def get_KM_distance(distance, unit):
    if unit.lower() == 'mi' or unit.lower() == 'miles':
        distance = convert_miles_to_km(distance)
    elif unit.lower() == 'm' or unit.lower() == 'meters':
        distance = distance/1000
    elif unit.lower() == 'ft' or unit.lower() == 'feet':
        distance = distance/3280.839895
    else:
        print("Change your unit to something more common: ", unit)
    
    return distance


def GeneratePolygon(lat, lon, radius=1, unit='km', sides=6):
    
    lat_rad = (lat/180)*math.pi 
    lon_rad = (lon/180)*math.pi
    
    if unit != 'km':
        radius = get_KM_distance(radius, unit)
        
    # in degrees, bearing is [30, 90, 150, 210, 270, 330] 
    # circle is 360 degrees
    interval = (2*math.pi)/sides
    # since we start at 0, first bearing splits middle/ interval/2
    # start = interval/2
    start = 0

    # For a Rect/Square Poly, the radius needs to adjust to keep area same as resolution
    if sides == 4:
        # easy trig, (res^2 + res^2)^0.5 = 2^0.5 * res
        radius = (round(radius*math.sqrt(2), 4))/2
        # start evaluating corner angle at 45 degrees
        start = interval/2
    else:
        start = 0
    
    radian_bearings = [start + i*interval for i in range(sides)]
    
    # latitude degrees
    # equator = 110.567 km
    # poles = 110.699 km
    # 1 degree latitde ~ 111km
    # R is Radius of the earth = 6731 km
    R = 6371
    d_over_R = radius/R
    
    polygon_points = []
    for radian in radian_bearings:
        new_lat_rad = math.asin(math.sin(lat_rad)*math.cos(d_over_R) + math.cos(lat_rad)*math.sin(d_over_R)*math.cos(radian))
        
        new_lon_rad = lon_rad + math.atan2((math.sin(radian)*math.sin(d_over_R)*math.cos(lat_rad)), (math.cos(d_over_R)-(math.sin(lat_rad)*math.sin(new_lat_rad)))) 

        new_lat = new_lat_rad * 180/math.pi
        new_lon = new_lon_rad * 180/math.pi
        polygon_points.append(({"latitude": new_lat, "longitude": new_lon}))

    return polygon_points   
    

def findAllPOIinRadius(center_lat, center_lon, all_poi_list, radius=0, units='km'):
    buffer_shape_poi_list = GeneratePolygon(center_lat, center_lon, radius, units, sides=6)   
    buffer_tuples = [(poi['longitude'], poi['latitude']) for poi in buffer_shape_poi_list]
    
    filtered_poi = []
    for poi in all_poi_list:
        if is_point_in_path(poi['x'], poi['y'], buffer_tuples):
            filtered_poi.append(poi)
            
    return filtered_poi


def test():
    find_me()


if __name__ == '__main__':
    test()