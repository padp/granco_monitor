from __future__ import print_function

import time
import os
import json
import math
from GetSendPressData import *
from datetime import datetime, timedelta
from pylogix.eip import PLC
import zstd
import zstandard as zstd_c
import sqlite3 as sl

MAX_FILE_CONTENT_LEN = 90
ITERATIONS_BETWEEN_UPLOADS = 5
FILE_CONTENTS = {}
INTERVAL_OBJECT = {}

saw_comm = PLC()
saw_comm.IPAddress = "10.4.20.21"


def read_tags_from_fp(fp):
    l = []
    with open(fp, 'r') as rdr:
        for line in rdr.readlines():
            if len(line) > 0:
                l.append(line.rstrip('\n'))
    return l


def add_tags_to_collection_obj(data):
    for line in data:
        key = line['tagName']
        value = line['value']
        if key not in FILE_CONTENTS:
            FILE_CONTENTS[key] = []
        FILE_CONTENTS[key].append(value)
    manage_collection_size()


def manage_hourly_sql_datas(data):
    for line in data:
        key = line['tagName']
        value = line['value']
        if key not in INTERVAL_OBJECT:
            INTERVAL_OBJECT[key] = []
        INTERVAL_OBJECT[key].append(value)


def save_sql_and_get_next():
    if "Date/Time" in INTERVAL_OBJECT:
        if len(INTERVAL_OBJECT['Date/Time']) > 2:
            last_datetime = datetime.strptime(
                INTERVAL_OBJECT["Date/Time"][-1], "%m/%d/%Y %H:%M:%S")
            second_last_datetime = datetime.strptime(
                INTERVAL_OBJECT['Date/Time'][-2], "%m/%d/%Y %H:%M:%S")
            if last_datetime.minute != second_last_datetime.minute:
                temp_new_interval_object = {
                    key: [INTERVAL_OBJECT[key][-1]] for key in INTERVAL_OBJECT}
                for key in INTERVAL_OBJECT:
                    INTERVAL_OBJECT[key].pop()
                zstd_io = zstd.compress(json.dumps(
                    INTERVAL_OBJECT).encode('gbk'))
                write_to_db(db_fp, zstd_io)
                return temp_new_interval_object
            else:
                return None
        else:
            return None
    else:
        return None


def all_equal(l):
    return l and all(l[0] == elem for elem in l)


def manage_collection_size():
    for key in FILE_CONTENTS:
        if len(FILE_CONTENTS[key]) > MAX_FILE_CONTENT_LEN:
            is_same_len = all_equal([len(FILE_CONTENTS[x])
                                    for x in list(FILE_CONTENTS.keys())])
            if is_same_len:
                try:
                    for key in FILE_CONTENTS:
                        del FILE_CONTENTS[key][0]
                except:
                    FILE_CONTENTS.clear()
                return None
            else:
                print('There is a problem')
                return None
        else:
            return None


def compress_existing_db(input_db_path, output_db_path):     
    # Open the existing SQLite database     
    with sl.connect(input_db_path) as input_conn:         
    # # Create a new SQLite database for the compressed data         
        with sl.connect(output_db_path) as output_conn:            
    #  # Create a Zstd compression context            
            cctx = zstd_c.ZstdCompressor(level=3)                        
    # # Iterate over each table in the input database             
            for table_name in input_conn.execute("SELECT name FROM sqlite_master WHERE type='table';"):                 
                table_name = table_name[0]                                
    #  # Read data from the input table                
                input_data = input_conn.execute(f"SELECT * FROM {table_name};").fetchall()                                
    #  # Compress the data using Zstd                 
                compressed_data = cctx.compress(sl.Binary(str(input_data).encode()))                                
    #  # Create a new table in the output database                
                output_conn.execute(f"CREATE TABLE {table_name} (compressed_data BLOB);")                                
    #  # Insert the compressed data into the new table                
                output_conn.execute(f"INSERT INTO {table_name} (compressed_data) VALUES (?);", (compressed_data,))                                
                print(f"Compressed and stored data for table: {table_name}")


def write_to_db(db_fp, data):
    now = datetime.now()
    columns = [x['keyName'] for x in data]
    values_to_add = [x['value'] for x in data]
    con = sl.connect(db_fp, detect_types=sl.PARSE_DECLTYPES |
                     sl.PARSE_COLNAMES)
    with con:
        cursor = con.cursor()
        insertQuery = f"""INSERT INTO RAW_DATA
    		({', '.join(f"{col}" for col in columns)}) VALUES ({', '.join(['?'] * len(columns))});"""
        cursor.execute(insertQuery, values_to_add)
        con.commit()
        cursor.close()


def read_from_db(db_fp, table_name, is_zstd):
    d = {}
    rdr = None
    con = sl.connect(db_fp)
    column_names = []
    with con:
        cursor = con.cursor()
        query = """SELECT * FROM {0}""".format(table_name)
        rdr = cursor.execute(query).fetchall()
        column_names = [description[0] for description in cursor.description]
        cursor.close()
    for entry in rdr:
        if is_zstd:
            key = entry[0]
            value = json.loads(zstd.decompress(entry[1]))
            d[key] = value
        else:
            for row_val_tuple in enumerate(entry):
                if column_names[row_val_tuple[0]] not in d:
                    d[column_names[row_val_tuple[0]]] = []
                d[column_names[row_val_tuple[0]]].append(row_val_tuple[1])
    return d


def create_sql_table(db_path, table_name, data):
    columns = [x['keyName'] for x in data]
    data_types = [type(x['value']).__name__ for x in data]
    con = sl.connect(
        db_path, detect_types=sl.PARSE_DECLTYPES | sl.PARSE_COLNAMES)
    with con:
        table = f""" CREATE TABLE IF NOT EXISTS {table_name} (
                {', '.join(f"{col} {data_type}" for col, data_type in zip(columns, data_types))}
            ); """
        con.execute(table)

def get_db_path():
    today = datetime.now()
    ext = '.db'
    daily_db_name =  f'../../LogixDatabase/Granco Saw/{today.year}/{today.month}'
    try:
        os.makedirs(daily_db_name)
    except:
        "directory exists"
    return f'{daily_db_name}/{today.month}-{today.day}{ext}'


if __name__ == '__main__':
    gathered_datas = []
    sub_tags = {
        'RNM': 'Recipe Name', 
        'BTH': 'Batch Height', 
        'BTW': 'Batch Width', 
        'PPC': 'Parts per Cut', 
        'BFR': 'Blade Feed Rate', 
        'CSQ': 'CSQ',
        'ATD': 'ATD', 
        'PCL': 'Part Cut Length',
        'CCS': 'CCS',
        'QTY': 'Quantity',
        'UNIT': 'Unit',
        'BGP': 'Back Gauge Pressure',
        'SCP': 'Side Clamp Pressure',
        'TCP': 'Top Clamp Pressure'}
    gathered_datas.append(','.join(list(sub_tags.values())))
    for i in range(0, 500):
        base_tag = f"RECIPE_STORED[{i}]"
        this_data = []
        for key in sub_tags:
            tag_to_read = f'{base_tag}.{key}'
            this_data.append(saw_comm.Read(tag_to_read).Value)
        gathered_datas.append(','.join([str(x) for x in this_data]))
    _=0
    print(gathered_datas)
"""
    with open(f'Granco_Recipes.csv', 'w') as csv_write:
        for line in gathered_datas:
            csv_write.write(line + '\n')
            """