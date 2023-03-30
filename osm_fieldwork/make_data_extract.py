#!/usr/bin/python3

# Copyright (c) 2022 Humanitarian OpenStreetMap Team
#
# This file is part of OSM-Fieldwork.
#
#     OSM-Fieldwork is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     OSM-Fieldwork is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with OSM-Fieldwork.  If not, see <https:#www.gnu.org/licenses/>.
#

import argparse
import os
import logging
import sys
import re
import yaml
import json
from geojson import Point, Feature, FeatureCollection, dump, Polygon
import geojson
from osm_fieldwork.filter_data import FilterData
from osm_fieldwork.xlsforms import xlsforms_path
import requests
# from requests.auth import HTTPBasicAuth
from io import BytesIO
import zipfile
import time
import psycopg2
from shapely.geometry import shape, Polygon
import overpy
import shapely


# all possible queries
choices = [
    "buildings",
    "amenities",
    "toilets",
    "landuse",
    "emergency",
    "shops",
    "waste",
    "water",
    "education",
    "healthcare",
]

# Instantiate logger
log_level = os.getenv("LOG_LEVEL", default="INFO")
logging.getLogger("urllib3").setLevel(log_level)
log = logging.getLogger(__name__)


class DatabaseAccess(object):
    def __init__(self, dbhost=None, dbname=None):
        self.dbshell = None
        self.dbcursor = None
        self.category = None
        if dbname == "underpass":
            # Authentication data
            # self.auth = HTTPBasicAuth(self.user, self.passwd)

            # Use a persistant connect, better for multiple requests
            self.session = requests.Session()
            self.url = "https://raw-data-api0.hotosm.org/v1"
            self.headers = {"accept": "application/json", "Content-Type": "application/json"}
        else:
            logging.info("Opening database connection to: %s" % dbhost)
            connect = "PG: dbname=" + dbname
            if dbhost is None or dbhost == "localhost":
                connect = f"dbname={dbname}"
            else:
                connect = f"host={dbhost} dbname={dbname}"
            try:
                self.dbshell = psycopg2.connect(connect)
                self.dbshell.autocommit = True
                self.dbcursor = self.dbshell.cursor()
                if self.dbcursor.closed == 0:
                    logging.info(f"Opened cursor in {dbname}")
            except Exception as e:
                logging.error("Couldn't connect to database: %r" % e)

    def createJson(self, category, boundary):
        path = xlsforms_path.replace("xlsforms", "data_models")
        file = open(f"{path}/{category}.yaml", "r").read()
        data = yaml.load(file, Loader=yaml.Loader)

        features = dict()
        features['geometry'] = boundary

        # The database tables to query
        # if tags exists, then only query those fields
        columns = dict()
        tags = data['where']['tags'][0]
        for tag, value in tags.items():
            if value == "not null":
                columns[tag] = []
        filters = {"tags": {"all_geometry": {"join_or": columns}}}
        features['filters'] = filters
        tables = list()
        for table in data['from']:
            if table == "nodes":
                tables.append("point")
            elif table == "ways_poly":
                tables.append("polygon")
            elif table == "ways_line":
                tables.append("linestring")
            elif table == "relations":
                pass
        features["geometryType"] = tables
        features["centroid"] = "true"
        return json.dumps(features)

    def createSQL(self, category):
        path = xlsforms_path.replace("xlsforms", "data_models")
        file = open(f"{path}/{category}.yaml", "r").read()
        data = yaml.load(file, Loader=yaml.Loader)

        sql = list()
        # The database tables to query
        tables = data['from']
        for table in tables:
            query = "SELECT "
            select = data['select']
            # if tags exists, then only return those fields
            if 'tags' in select:
                for tag in select['tags']:
                    query += f" {select[tag]} AS {tag}, "
                query += "osm_id AS id, ST_AsEWKT(ST_Centroid(geom)) "
            else:
                query += "osm_id AS id, ST_AsEWKT(ST_Centroid(geom)), tags "
            query += f" FROM {table} "
            where = data['where']
            # if tags exists, then only query those fields
            if 'where' in data:
                query += " WHERE "
                tags = data['where']['tags'][0]
                for tag, value in tags.items():
                    if value == "not null":
                        query += f"tags->>\'{tag}\' IS NOT NULL OR "
            sql.append(query[:-4])
        return sql

    def queryLocal(self, query, wkt):
        sql = f"DROP VIEW IF EXISTS ways_view;CREATE TEMP VIEW ways_view AS SELECT * FROM ways_poly WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{wkt.wkt}'), geom)"
        self.dbcursor.execute(sql)
        sql = f"DROP VIEW IF EXISTS nodes_view;CREATE TEMP VIEW nodes_view AS SELECT * FROM nodes WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{wkt.wkt}'), geom)"
        self.dbcursor.execute(sql)

        sql = f"DROP VIEW IF EXISTS relations_view;CREATE TEMP VIEW relations_view AS SELECT * FROM nodes WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{wkt.wkt}'), geom)"
        # self.dbcursor.execute(sql)

        if query.find(" ways_poly ") > 0:
            query = query.replace("ways_poly", "ways_view")
        elif query.find(" nodes ") > 0:
            query = query.replace("nodes", "nodes_view")
        features = list()
        log.debug(query)
        self.dbcursor.execute(query)
        result = self.dbcursor.fetchall()
        logging.info("Query returned %d records" % len(result))
        for item in result:
            gps = item[1][16:-1].split(" ")
            poi = Point((float(gps[0]), float(gps[1])))
            tags = item[2]
            tags["id"] = item[1]
            if "name" in tags:
                tags["title"] = tags["name"]
                tags["label"] = tags["name"]
            else:
                tags["title"] = None
                tags["label"] = None
            features.append(Feature(geometry=poi, properties=tags))
        return features

    def queryRemote(self, query):
        url = f"{self.url}/snapshot/"
        result = self.session.post(url, data=query, headers=self.headers)
        task_id = result.json()['task_id']
        newurl = f"{self.url}/tasks/status/{task_id}"
        while True:
            result = self.session.get(newurl, headers=self.headers)
            if result.json()['status'] == "PENDING":
                log.debug("Retrying...")
                time.sleep(1)
            elif result.json()['status'] == "SUCCESS":
                break
        zip = result.json()['result']['download_url']
        result = self.session.get(zip, headers=self.headers)
        fp = BytesIO(result.content)
        zfp = zipfile.ZipFile(fp, "r")
        zfp.extract("Export.geojson", "/tmp/")
        # Now take that taskid and hit /tasks/status url with get
        return zfp.read("Export.geojson")

class PostgresClient(DatabaseAccess):
    """Class to handle SQL queries for the categories"""
    def __init__(self, dbhost=None, dbname=None, output=None):
        """Initialize the postgres handler"""
        # OutputFile.__init__( self, output)
        self.boundary = None
        super().__init__(dbhost, dbname)

    def getFeatures(self, boundary=None, filespec=None, category="buildings"):
        """Extract buildings from Postgres"""
        logging.info("Extracting features from Postgres...")

        if type(boundary) != dict:
            clip = open(boundary, "r")
            geom = geojson.load(clip)
            if 'features' in geom:
                poly = geom['features'][0]['geometry']
            else:
                poly = geom["geometry"]
        else:
            poly = boundary
        wkt = shape(poly)

        if self.dbshell:
            # features = list()
            sql = self.createSQL(category)
            for query in sql:
                result = self.queryLocal(query, wkt)
            collection = FeatureCollection(result)
        else:
            request = self.createJson(category, poly)
            collection = self.queryRemote(request)

        cleaned = FilterData()
        models = xlsforms_path.replace("xlsforms", "data_models")
        # cleaned.parse(f"{models}/Impact Areas - Data Models V1.1.xlsx")
        file = f"{xlsforms_path}/{category}.xls"
        if os.path.exists(file):
            cleaned.parse(f"{xlsforms_path}/{category}.xls")
        else:
            cleaned.parse(f"{file}x")
        new = cleaned.cleanData(collection)
        jsonfile = open(filespec, "w")
        dump(new, jsonfile)
        return new

class OverpassClient(object):
    """Class to handle Overpass queries"""
    def __init__(self, output=None):
        """Initialize Overpass handler"""
        self.overpass = overpy.Overpass()
        #OutputFile.__init__(self, output)

    def getFeatures(self, boundary=None, filespec=None, category="buildings"):
        """Extract buildings from Overpass"""
        logging.info("Extracting features...")

        clip = open(boundary, "r")
        geom = geojson.load(clip)
        if 'features' in geom:
            aoi = geom['features'][0]['geometry']
        else:
            aoi = geom["geometry"]
        wkt = shape(aoi)
        poly = ""
        lat, lon = wkt.exterior.coords.xy
        index = 0
        while index < len(lat):
            poly += f"{lon[index]} {lat[index]} "
            index += 1

        query = (f'[out:json];way[\"building\"](poly:\"{poly[:-1]}\");(._;>;);out body geom;')
        result = self.overpass.query(query)
        features = list()
        for way in result.ways:
            poly = list()
            for coords in way.attributes['geometry']:
                lat = coords['lat']
                lon = coords['lon']
                point = [lon, lat]
                poly.append(point)
            exterior = Polygon(poly)
            center = shapely.centroid(exterior)
            features.append(Feature(geometry=center, properties=way.tags))

        collection = FeatureCollection(features)

        cleaned = FilterData()
        file = f"{xlsforms_path}/{category}.xls"
        if os.path.exists(file):
            cleaned.parse(f"{xlsforms_path}/{category}.xls")
        else:
            cleaned.parse(f"{file}x")
        new = cleaned.cleanData(collection)
        jsonfile = open(filespec, "w")
        dump(new, jsonfile)
        return new

class FileClient(object):
    """Class to handle Overpass queries"""

    def __init__(self, infile=None, output=None):
        """Initialize Overpass handler"""
        OutputFile.__init__(self, output)
        self.infile = infile

    def getFeatures(self, boundary=None, infile=None, outfile=None):
        """Extract buildings from a disk file"""
        logging.info("Extracting buildings from %s..." % infile)
        if boundary:
            poly = ogr.Open(boundary)
            layer = poly.GetLayer()
            ogr.Layer.Clip(osm, layer, memlayer)
        else:
            layer = poly.GetLayer()

        tmp = ogr.Open(infile)
        layer = tmp.GetLayer()

        layer.SetAttributeFilter("tags->>'building' IS NOT NULL")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Make GeoJson data file for ODK from OSM"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    parser.add_argument(
        "-o", "--overpass", action="store_true", help="Use Overpass Turbo"
    )
    parser.add_argument(
        "-p", "--postgres", action="store_true", help="Use a postgres database"
    )
    parser.add_argument(
        "-g", "--geojson", default="tmp.geojson", help="Name of the GeoJson output file"
    )
    parser.add_argument("-i", "--infile", help="Input data file")
    parser.add_argument("-dn", "--dbname", help="Database name")
    parser.add_argument("-dh", "--dbhost", default="localhost", help="Database host")
    parser.add_argument(
        "-b", "--boundary", help="Boundary polygon to limit the data size"
    )
    parser.add_argument(
        "-c",
        "--category",
        default="buildings",
        choices=choices,
        help="Which category to extract",
    )
    args = parser.parse_args()

    # if verbose, dump to the terminal.
    if args.verbose is not None:
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        ch.setFormatter(formatter)
        root.addHandler(ch)

    if args.geojson == "tmp.geojson":
        # The data file name is in the XML file
        regex = r"jr://file.*\.geojson"
        outfile = None
        filename = args.category + ".xml"
        if not os.path.exists(filename):
            logging.error("Please run xls2xform or make to produce %s" % filename)
            quit()
        with open(filename, "r") as f:
            txt = f.read()
            match = re.search(regex, txt)
            if match:
                tmp = match.group().split("/")
        outfile = tmp[3]
    else:
        outfile = args.geojson

    if args.postgres:
        logging.info("Using a Postgres database for the data source")
        pg = PostgresClient(args.dbhost, args.dbname, outfile)
        pg.getFeatures(args.boundary, args.geojson, args.category)
        # pg.cleanup(outfile)
    elif args.overpass:
        logging.info("Using Overpass Turbo for the data source")
        op = OverpassClient(outfile)
        op.getFeatures(args.boundary, args.geojson, args.category)
    elif args.infile:
        f = FileClient(args.infile)
        f.getFeatures(args.boundary, args.geojson, args.category)
        logging.info("Using file %s for the data source" % args.infile)
    else:
        logging.error("You need to supply either --overpass or --postgres")

        logging.info("Wrote output data file to: %s" % outfile)