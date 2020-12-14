# ee_s5p_plugin
QGIS Python Plugin for Sentinel 5 Precursor Satellite Data from Google Earth Engine

The Earth Engine Sentinel 5-Precursor plugin is a Python based GUI application to ad hoc Google’s vast catalog of remote sensing and satellite image data into the open source platform QGIS.

The GUI allows the user to build requests to the database without needing to manually type in string arguments, or copying and pasting dataset ID’s from the various individual webpages where this information is currently spread.

The ability to set filter parameters that work agnostically, can severely improve the efficiency of conducting analysis with multiple types of datasets. The GUI can also allow the user to manually select a resolution to extract the data, in order to overcome the Request Size limits set on Google’s free API.

The GUI will automate previewing the dataset in order to recommend dates, areas, tags, and read the description to learn more about any particular dataset where Google has included such information. The GUI also will attempt to handle failed requests in order to give the user recommendations on how to scale down their request to successfully return data on the next extraction.

Files are saved in text based, non-proprietary formats, GeoJSON, JSON, and CSV, so that the data can be automatically added as a vector layer, analyzed as a temporal dataset, and viewed in tabular format in and out of QGIS.

The original intent of this tool was to collect more Atmospheric Measurements from freely available datasets, and conduct analysis to calculate complicated marginal emissions factors using a large range of features (wind speed, weather, time of year, land categorization). I hope that others may find this tool exciting and useful for tackling a variety of complicated and vital data analysis projects.



Brandon Lee Crosbie
