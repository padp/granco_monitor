import time
import os
import json
import math
from GetGrancoSawData_v3 import read_from_db
from GetSendPressData import *
from datetime import datetime, timedelta
import zstd
import sqlite3
import numpy as np

BLADE_RETURN_SPEED = 300 

def update_record(record):
    record["first_break_time"] = (datetime.fromisoformat(record["shift_start_time"]) + timedelta(hours=2, minutes=40)).isoformat() 
    record["second_break_time"] = (datetime.fromisoformat(record["shift_start_time"]) + timedelta(hours=5, minutes=10)).isoformat() 
    record["clean_up_time"] = (datetime.fromisoformat(record["shift_start_time"]) + timedelta(hours=7, minutes=40)).isoformat()
    return record 


def sort_into_shifts(data_dict):
    shifts = {}

    for part_number, records in data_dict.items():
        for record in records:
            datetime_str = record.get("isotime")
            shift_date = get_shift(datetime_str)
            shift_start_time = ""
            if datetime_str is not None:
                [year, month, day] =[ datetime.fromisoformat(datetime_str).year, datetime.fromisoformat(datetime_str).month, datetime.fromisoformat(datetime_str).day ]
                record["shift_date"] = shift_date
                if datetime.fromisoformat(datetime_str).time() >= datetime(1900, 1, 1, 22, 50).time():
                    if f"{shift_date} - Third Shift" not in shifts:
                        shifts[f"{shift_date} - Third Shift"] = {}
                    if part_number not in shifts[f"{shift_date} - Third Shift"]:
                        shifts[f"{shift_date} - Third Shift"][part_number] = []
                    record["shift_start_time"] = (datetime(year, month, day, 0, 0, 0) + timedelta(hours=22, minutes=50)).isoformat()
                    record = update_record(record)
                    shifts[f"{shift_date} - Third Shift"][part_number].append(
                        record)
                elif datetime.fromisoformat(datetime_str).time() < datetime(1900, 1, 1, 6, 50).time():
                    if f"{shift_date} - Third Shift" not in shifts:
                        shifts[f"{shift_date} - Third Shift"] = {}
                    if part_number not in shifts[f"{shift_date} - Third Shift"]:
                        shifts[f"{shift_date} - Third Shift"][part_number] = []
                    record["shift_start_time"] = (datetime(year, month, day, 0, 0, 0) - timedelta(hours=1, minutes=10)).isoformat()
                    record = update_record(record)
                    shifts[f"{shift_date} - Third Shift"][part_number].append(
                        record)
                elif datetime.fromisoformat(datetime_str).time() >= datetime(1900, 1, 1, 14, 50).time() and datetime.fromisoformat(datetime_str).time() < datetime(1900, 1, 1, 22, 50).time():
                    if f"{shift_date} - Second Shift" not in shifts:
                        shifts[f"{shift_date} - Second Shift"] = {}
                    if part_number not in shifts[f"{shift_date} - Second Shift"]:
                        shifts[f"{shift_date} - Second Shift"][part_number] = []
                    record["shift_start_time"] = (datetime(year, month, day, 0, 0, 0) + timedelta(hours=14, minutes=50)).isoformat()
                    record = update_record(record)
                    shifts[f"{shift_date} - Second Shift"][part_number].append(
                        record)
                else:
                    if f"{shift_date} - First Shift" not in shifts:
                        shifts[f"{shift_date} - First Shift"] = {}
                    if part_number not in shifts[f"{shift_date} - First Shift"]:
                        shifts[f"{shift_date} - First Shift"][part_number] = []
                    record["shift_start_time"] = (datetime(year, month, day, 0, 0, 0) + timedelta(hours=6, minutes=50)).isoformat()
                    record = update_record(record)
                    shifts[f"{shift_date} - First Shift"][part_number].append(
                        record)

    return get_part_multiple(shifts)


def find_datetime_index(target_datetime, datetime_list):
    for i, dt in enumerate(datetime_list):
        if dt == target_datetime:
            return i
    return None


def get_part_multiple(d):
    for shift in d:
        times_lists = []
        theoretical_elapsed_lists = []
        actual_elapsed_lists = []
        total_batch_reloads = []
        #[i_shift_start_time, i_first_break_time, i_second_break_time, i_clean_up_time] = ["","","",""]
        for part_number in d[shift]:
            times_lists.extend([datetime.fromisoformat(x['isotime']) for x in d[shift][part_number]])
            theoretical_elapsed_lists.extend([x['theoretical_cut_time'] for x in d[shift][part_number]])
            actual_elapsed_lists.extend([x[1]['elapsed_seconds'] if x[0] > 0 else 0 for x in enumerate(d[shift][part_number])])
            cut_numbers = sorted([x['cut_number'] for x in d[shift][part_number]])
            consensus_max_cut_number = np.percentile(cut_numbers, 99.5)
            part_multiple = math.floor(consensus_max_cut_number) - 1
            batch_reload_lists = [x['cut_number'] for x in d[shift][part_number] if x['cut_number'] == part_multiple]
            batch_reload_time_elasped_lists = [x['elapsed_seconds'] for x in d[shift][part_number] if x['cut_number'] == part_multiple + 1]
            i_shift_start_time = datetime.fromisoformat(d[shift][part_number][0]['shift_start_time'])
            i_first_break_time = datetime.fromisoformat(d[shift][part_number][0]["first_break_time"])
            i_second_break_time = datetime.fromisoformat(d[shift][part_number][0]["second_break_time"])
            i_clean_up_time = datetime.fromisoformat(d[shift][part_number][0]["clean_up_time"])
            for entry in enumerate(d[shift][part_number]):
                d[shift][part_number][entry[0]]['cut_multiple'] = part_multiple
        a_last_cut_time = max(times_lists, default=None)
        a_first_cut_time = min(times_lists, default=None)
        if a_last_cut_time != None:
            def get_break_time_seconds():
                now = datetime.now()
                break_time = 0
                if now > i_first_break_time:
                    break_time = 10 * 60
                elif now > i_second_break_time:
                    break_time = 40 * 60
                elif now > i_clean_up_time:
                    break_time = 60 * 60
                return break_time
            break_time_seconds = get_break_time_seconds()
            a_first_break_time = max([dt for dt in times_lists if dt < i_first_break_time], default=None) if a_last_cut_time > i_first_break_time else None
            a_second_break_time = max([dt for dt in times_lists if dt < i_second_break_time], default=None) if a_last_cut_time > i_second_break_time else None
            a_end_first_break_time = times_lists[find_datetime_index(a_first_break_time, times_lists) + 1] if find_datetime_index(a_first_break_time, times_lists) != None and len(times_lists) > find_datetime_index(a_first_break_time, times_lists) + 1 else None
            a_end_second_break_time = times_lists[find_datetime_index(a_second_break_time, times_lists) + 1] if find_datetime_index(a_second_break_time, times_lists) != None and len(times_lists) > find_datetime_index(a_second_break_time, times_lists) + 1 else None
            a_clean_up_time = a_last_cut_time if datetime.now() > i_clean_up_time else None
            d[shift]['shift_summary'] = {
                'scheduled_constants': {
                    '_shift_start': i_shift_start_time.isoformat(),
                    '_first_break': i_first_break_time.isoformat(),
                    '_second_break': i_second_break_time.isoformat(),
                    '_clean_up': i_clean_up_time.isoformat(),
                    '_shift_end': (i_shift_start_time + timedelta(hours=8)).isoformat(),
                },
                'total_ideal_break_time': break_time_seconds,
                'first_break_time': a_first_break_time.isoformat() if a_first_break_time != None else None,
                'second_break_time': a_second_break_time.isoformat() if a_second_break_time != None else None,
                'first_break_end_time': a_end_first_break_time.isoformat() if a_end_first_break_time != None else None,
                'second_break_end_time': a_end_second_break_time.isoformat() if a_end_second_break_time != None else None,
                'clean_up_time': a_clean_up_time.isoformat() if a_clean_up_time != None else None,
                'first_cut_time': a_first_cut_time.isoformat() if a_first_cut_time != None else None,
                'last_cut_time': a_last_cut_time.isoformat() if a_last_cut_time != None else None,
                'first_break_elapsed_seconds': (a_end_first_break_time - a_first_break_time).total_seconds() if a_end_first_break_time != None and a_first_break_time != None else None,
                'second_break_elapsed_seconds': (a_end_second_break_time - a_second_break_time).total_seconds() if a_second_break_time != None and a_end_second_break_time != None else None,
                'start_up_lag_elapsed_seconds': (a_first_cut_time - i_shift_start_time).total_seconds() if a_first_cut_time != None and i_shift_start_time != None else None,
                'end_shift_lag_elapsed_seconds': ((i_shift_start_time + timedelta(hours=7, minutes=40)) - a_clean_up_time).total_seconds() if i_shift_start_time != None and a_clean_up_time != None else None, 
                'count_batch_reload_cycles': len(batch_reload_lists),
            }
            #d[shift]['shift_summary']['total_working_time_seconds']

            pseudo_times_lists = times_lists
            pseudo_times_lists.insert(0, i_shift_start_time)
            pseudo_times_lists.append((i_shift_start_time + timedelta(hours=8)))
            d[shift]['shift_summary']['sum_theoretical_elapsed_seconds'] = sum(theoretical_elapsed_lists)
            d[shift]['shift_summary']['sum_actual_elapsed_seconds'] = sum(actual_elapsed_lists)
            d[shift]['shift_summary']['sum_batch_reload_elapsed_seconds'] = len(batch_reload_lists) * 45
            d[shift]['shift_summary']['net_shift_efficiency'] = (sum(theoretical_elapsed_lists) + break_time_seconds + (len(batch_reload_lists) * 45)) / sum(actual_elapsed_lists)
            d[shift]['shift_summary']['downtime']= {
                'datetime': [pseudo_times_lists[x[0] - 1].isoformat() for x in enumerate(pseudo_times_lists) if x[0] > 0 and (pseudo_times_lists[x[0]] - pseudo_times_lists[x[0] - 1]).total_seconds() > 300],
                'elapsed_seconds': [(pseudo_times_lists[x[0]] - pseudo_times_lists[x[0] - 1]).total_seconds() for x in enumerate(pseudo_times_lists) if x[0] > 0 and (pseudo_times_lists[x[0]] - pseudo_times_lists[x[0] - 1]).total_seconds() > 300]
            } 
           # print((sum(theoretical_elapsed_lists) + (len(batch_reload_lists) * 45)) / (sum(actual_elapsed_lists) - ((10 + 30 + 20) * 60)))
            print([shift, ((sum(theoretical_elapsed_lists) + (len(batch_reload_lists) * 45))) / 3600])
            print([np.mean(actual_elapsed_lists), sum(actual_elapsed_lists) / 3600])
            _=0
    return d

def get_shift(datetime_str):
    datetime_obj = datetime.fromisoformat(datetime_str)
    # Third Shift + 1 day
    if datetime.fromisoformat(datetime_str).time() >= datetime(1900, 1, 1, 22, 50).time():
        return (datetime_obj + timedelta(hours=1.5)).strftime('%m-%d-%Y')
    # Third Shift Current Day
    elif datetime.fromisoformat(datetime_str).time() < datetime(1900, 1, 1, 6, 50).time():
        return datetime_obj.strftime('%m-%d-%Y')
    elif datetime.fromisoformat(datetime_str).time() >= datetime(1900, 1, 1, 14, 50).time() and datetime.fromisoformat(datetime_str).time() < datetime(1900, 1, 1, 22, 50).time():  # Second Shift
        return datetime_obj.strftime('%m-%d-%Y')
    else:  # First Shift
        return datetime_obj.strftime('%m-%d-%Y')


def find_lowest_local_minima_prior_to_maxima(lst, maxima_indices):
    local_minima = []

    for i in range(1, len(maxima_indices)):
        start_index = maxima_indices[i - 1]
        end_index = maxima_indices[i]

        lowest_minima = float('inf')  # Initialize with a large value

        for j in range(start_index, end_index):
            if lst[j - 1] > lst[j] < lst[j + 1]:  # Check for local minima
                if lst[j] < lowest_minima:  # Update lowest_minima if a lower local minimum is found
                    lowest_minima = lst[j]

        # Check if a local minimum was found in the range
        if lowest_minima != float('inf'):
            local_minima.append(lowest_minima)
        elif len(local_minima) > 0:
            local_minima.append(local_minima[-1])
        else:
            local_minima.append(0)

    return local_minima


def calculate_operations(d):
    operations_dict = {}
    feed_rate_list = d.get("Current_Blade_Feed_Rate", [])
    date_time_list = d.get("DateTime", [])
    saw_pos_list = d.get("Sawblade_Position", [])
    gauge_pos_list = d.get("Backgauge_Position", [])
    cut_length_list = d.get("Display_Cut_Length", [])
    part_number_lists = d.get("Current_Recipe", [])

    distinct_gauge_positions = list(set(gauge_pos_list))
    if len(distinct_gauge_positions) > 20:
        if len(feed_rate_list) == len(saw_pos_list) and len(date_time_list) == len(saw_pos_list):
            local_maxima_indices = [i for i in range(1, len(saw_pos_list) - 1)
                                    if saw_pos_list[i] > saw_pos_list[i - 1] and saw_pos_list[i] > saw_pos_list[i + 1] and saw_pos_list[i] > 20]
            print(len(local_maxima_indices))
            if local_maxima_indices:
                local_maxima = [saw_pos_list[i]
                                for i in local_maxima_indices]
                local_maxima_times = [date_time_list[i]
                                    for i in local_maxima_indices]
                local_maxima_part_numbers = [
                    part_number_lists[i] for i in local_maxima_indices]
                local_maxima_elapsed_seconds = [(
                    datetime.strptime(local_maxima_times[i], "%m/%d/%Y %H:%M:%S") -
                    datetime.strptime(
                        local_maxima_times[i-1], "%m/%d/%Y %H:%M:%S")
                ).total_seconds() for i in range(1, len(local_maxima_times))]
                local_maxima_gauge_positions = [
                    gauge_pos_list[i] for i in local_maxima_indices]
                local_maxima_cut_lengths = [cut_length_list[i]
                                            for i in local_maxima_indices]
                local_maxima_elapsed_seconds.insert(
                    0, min(local_maxima_elapsed_seconds))
                local_minima = find_lowest_local_minima_prior_to_maxima(
                    saw_pos_list, local_maxima_indices)
                local_minima.insert(0, 0)
                blade_return_times = [((saw_pos_list[local_maxima_indices[i[0]]] - local_minima[i[0] + 1]) / BLADE_RETURN_SPEED) if i[0] < len(local_maxima_indices) - 1  else 0 for i in enumerate(local_maxima_indices)] 
                theoretical_times = [((saw_pos_list[local_maxima_indices[i[0]]] - local_minima[i[0]]
                                    ) / feed_rate_list[i[1]]) + blade_return_times[i[0]] for i in enumerate(local_maxima_indices)]
                for i in range(len(local_maxima)):
                    part_number = local_maxima_part_numbers[i]
                    if part_number not in operations_dict:
                        operations_dict[part_number] = []
                    operations_dict[part_number].append(
                        {
                            "local_maxima": local_maxima[i],
                            "time_at_maxima": local_maxima_times[i],
                            "local_minima": local_minima[i],
                            "isotime": datetime.strptime(local_maxima_times[i], "%m/%d/%Y %H:%M:%S").isoformat(),
                            "elapsed_seconds": local_maxima_elapsed_seconds[i],
                            "theoretical_cut_time": math.ceil(theoretical_times[i] * 60),
                            "gauge_position": local_maxima_gauge_positions[i],
                            "cut_length": local_maxima_cut_lengths[i],
                            "is_trim_cut": False if i == 0 else local_maxima_gauge_positions[i] > local_maxima_gauge_positions[i - 1],
                            "cut_number": math.floor(local_maxima_gauge_positions[i] / (local_maxima_cut_lengths[i] / 25.4))
                        })
            else:
                print(
                    f"No local maxima found for Part_Number {part_number}.")
        else:
            print(
                f"Warning: 'Current_Blade_Feed_Rate' and/or 'DateTime' list does not have the same length as 'Sawblade_Position' list for Current_Recipe {part_number}.")

    return operations_dict


def get_data_to_compress(db_path):
    # Connect to the SQLite database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get the column names of the table to determine the DateTime column
    # Replace 'your_table_name' with the actual table name
    table_name = 'your_table_name'
    cursor.execute(f"PRAGMA table_info({table_name})")
    column_names = [col[1] for col in cursor.fetchall()]

    if 'DateTime' not in column_names:
        print("Error: The 'DateTime' column does not exist in the table.")
        conn.close()
        return None

    # Get the current datetime minus 24 hours
    threshold = datetime.now() - timedelta(hours=24)
    threshold_str = threshold.isoformat()

    # Get the data to compress
    cursor.execute(
        f"SELECT * FROM {table_name} WHERE DateTime >= ?", (threshold_str,))
    data = cursor.fetchall()

    # Convert the data to a JSON-like list of dictionaries
    data_dict = []
    for row in data:
        data_dict.append(dict(zip(column_names, row)))

    conn.close()
    return data_dict


def compress_data(data_dict):
    # Convert the data dictionary to a JSON string
    import json
    data_json = json.dumps(data_dict)

    # Compress the JSON data using zstd
    # You can adjust the compression level as needed
    compressor = zstd.ZstdCompressor(level=3)
    compressed_data = compressor.compress(data_json)

    return compressed_data


def remove_old_entries(db_path):
    # Connect to the SQLite database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get the column names of the table to determine the DateTime column
    # Replace 'your_table_name' with the actual table name
    table_name = 'your_table_name'
    cursor.execute(f"PRAGMA table_info({table_name})")
    column_names = [col[1] for col in cursor.fetchall()]

    if 'DateTime' not in column_names:
        print("Error: The 'DateTime' column does not exist in the table.")
        conn.close()
        return

    # Get the current datetime minus 24 hours
    threshold = datetime.now() - timedelta(hours=24)
    threshold_str = threshold.isoformat()

    # Remove old entries from the database
    cursor.execute(
        f"DELETE FROM {table_name} WHERE DateTime < ?", (threshold_str,))
    conn.commit()

    conn.close()
    print("Old entries removal completed.")


def are_nested_dictionaries_equal(dict1, dict2):
    if isinstance(dict1, dict) and isinstance(dict2, dict):
        if set(dict1.keys()) != set(dict2.keys()):
            return False
        for key in dict1.keys():
            if not are_nested_dictionaries_equal(dict1[key], dict2[key]):
                return False
        return True
    else:
        return dict1 == dict2


def create_update_summary_sql_table(db_path, table_name, data):
    reform_existing_datas = {}
    existing_datas = read_from_db(
        '../../LogixDatabase/gcs-shift-summary.db', table_name, False)
    con = sqlite3.connect(
        db_path, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    cursor = con.cursor()
    with con:
        cursor.execute(f""" 
            CREATE TABLE IF NOT EXISTS {table_name} (
                id TEXT PRIMARY KEY,
                data_json TEXT
            )
            """)
        insert_command = f"""INSERT INTO {table_name} (id, data_json) VALUES (?, ?)"""
        for ds in data:
            if 'id' not in existing_datas:
                cursor.execute(insert_command, (ds, json.dumps(data[ds])))
            else:
                reform_existing_datas = {existing_datas['id'][x[0]]: json.loads(existing_datas['data_json'][x[0]]) for x in enumerate(
                    existing_datas['id'])} if reform_existing_datas == {} else reform_existing_datas
                if ds not in reform_existing_datas:
                    insert_command = f"""INSERT INTO {table_name} (id, data_json) VALUES (?, ?)"""
                    cursor.execute(insert_command, (ds, json.dumps(data[ds])))
                else:
                    if not are_nested_dictionaries_equal(reform_existing_datas[ds], data[ds]):
                        insert_command = f"""UPDATE {table_name} SET data_json = (?) WHERE id = (?)"""
                        cursor.execute(
                            insert_command, (json.dumps(data[ds]), ds))
                    else:
                        print(
                            (ds, 'The dictionaries are equal so no updating necessary'))
        con.commit()


def write_results_to_json(data):
    with open('./gcs-shift-summary.json', 'w') as js_write:
        json.dump(data, js_write)
    return './gcs-shift-summary.json'

def get_shift_from_date(datetime_str=None):
    def format_date(date):
        return date.strftime("%Y-%m-%d %H:%M:%S")

    datetime_obj = datetime.fromisoformat(datetime_str) if datetime_str else datetime.now()
    current_hour = datetime_obj.hour
    current_minute = datetime_obj.minute

    second_shift_start_time = (14, 50)
    second_shift_end_time = (22, 50)
    third_shift_start_time = (22, 50)
    third_shift_end_time = (6, 50)

    current_date = datetime_obj

    if (
        current_hour > third_shift_start_time[0]
        or (current_hour == third_shift_start_time[0] and current_minute >= third_shift_start_time[1])
    ):
        # Third Shift + 1 day
        current_date += timedelta(hours=1, minutes=30)
        return format_date(current_date) + " - Third Shift"
    elif (
        current_hour < third_shift_end_time[0]
        or (current_hour == third_shift_end_time[0] and current_minute < third_shift_end_time[1])
    ):
        # Third Shift Current Day
        return format_date(current_date) + " - Third Shift"
    elif (
        second_shift_start_time[0] < current_hour < second_shift_end_time[0]
        or (
            second_shift_start_time[0] == current_hour
            and current_minute >= second_shift_start_time[1]
        )
        or (
            current_hour == second_shift_end_time[0]
            and current_minute < second_shift_end_time[1]
        )
    ):
        # Second Shift
        return format_date(current_date) + " - Second Shift"
    else:
        # First Shift
        return format_date(current_date) + " - First Shift"


# mime_type = "application/octet-stream"
# directory_id = '1BhfzXXzMAhqtRf61EEo6qDEL51XKUcDG'
# drive_service_and_dir = get_drive_folders('GCS_V3')
# db_fp = '../../LogixDatabase/local-gcs-v3-xfer.db'
# r_start_time = datetime.now()
if __name__ == "__main__":
    n = datetime.now()
    base_dir = "C:/Users/tloy/Documents/LogixDatabase/Granco Saw"
    [yesterday_date, current_date] = [n - timedelta(days=1), n] 
    [yesterday_dir, current_dir] = [os.path.join(base_dir, f"{yesterday_date.year}/{yesterday_date.month}"), os.path.join(base_dir, f"{current_date.year}/{current_date.month}")]
    t = os.listdir(current_dir)
    db_fps = list(set([os.path.join(x, y) for x in [current_dir, yesterday_dir] for y in os.listdir(x)]))
    for db_fp in db_fps:
        read = read_from_db(db_fp, 'RAW_DATA', False)
        calc = calculate_operations(read)
        if calc:
            results = sort_into_shifts(calc)
            json_fp = write_results_to_json(results)
            create_update_summary_sql_table(
                '../../LogixDatabase/gcs-shift-summary.db', 'Summary_By_Shift', results)