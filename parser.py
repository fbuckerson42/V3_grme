import os
import re
import json
import requests
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
SPREADSHEET_URL = os.getenv("SPREADSHEET_URL", "").strip()


def get_csv_url(spreadsheet_url):
    match = re.search(r'/d/([a-zA-Z0-9_-]+)/', spreadsheet_url)
    if not match:
        raise ValueError("Не вдалося знайти ID таблички")

    spreadsheet_id = match.group(1)
    gid_match = re.search(r'gid=(\d+)', spreadsheet_url)
    gid = gid_match.group(1) if gid_match else "0"

    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tqx=out:csv&gid={gid}"


def parse_csv_line(line):
    row = []
    in_quotes = False
    current = ""
    for char in line:
        if char == '"':
            in_quotes = not in_quotes
        elif char == ',' and not in_quotes:
            row.append(current.strip())
            current = ""
        else:
            current += char
    row.append(current.strip())
    return row


def parse_csv(csv_url):
    response = requests.get(csv_url, timeout=30)
    response.raise_for_status()

    content = response.content.decode('utf-8-sig')
    lines = content.strip().split('\n')
    rows = [parse_csv_line(line) for line in lines]

    if len(rows) < 2:
        raise ValueError("Табличка порожня або немає даних")

    header = rows[0]
    data_rows = rows[1:]

    date_cols = {}
    for i, col in enumerate(header):
        col = col.strip().strip('"')
        if re.match(r'\d{2}\.\d{2}', col):
            date_cols[col] = i

    if not date_cols:
        raise ValueError("Не знайдено колонки з датами (формат ДД.ММ)")

    return header, data_rows, date_cols


def get_all_tables(data_rows):
    tables = {}
    last_table = None

    for row in data_rows:
        if len(row) < 5:
            continue

        cell_0 = row[0].strip().strip('"')
        cell_1 = row[1].strip().strip('"')
        cell_2 = row[2].strip().strip('"')
        cell_3 = row[3].strip().strip('"')

        current = None
        if cell_1:
            last_table = int(cell_1)
            current = last_table
        elif cell_0 and last_table:
            current = last_table

        if current and cell_2 and 1 <= current <= 8:
            if current not in tables:
                tables[current] = []
            tables[current].append({"name": cell_2, "telegram": cell_3 if cell_3 else None})

    return tables


def determine_status(cell_value):
    cell = cell_value.strip().strip('"') if cell_value else ""
    if cell == "✓":
        return None
    if cell == "" or cell == " ":
        return True
    if cell.lower() == "х":
        return False
    return None


def build_schedule(csv_url):
    header, data_rows, date_cols = parse_csv(csv_url)
    all_tables = get_all_tables(data_rows)

    schedule = {}

    for date_col, col_idx in date_cols.items():
        employees_by_table = {}

        for table_num in range(1, 9):
            employees = all_tables.get(table_num, [])

            result_employees = []
            for emp in employees:
                emp_name = emp["name"]
                cell_value = None

                for row in data_rows:
                    row_name = row[2].strip().strip('"') if len(row) > 2 else ""
                    if row_name == emp_name and col_idx < len(row):
                        cell_value = row[col_idx]
                        break

                status = determine_status(cell_value)
                if status is not None:
                    result_employees.append({
                        "name": emp_name,
                        "status": status
                    })

            if result_employees:
                employees_by_table[f"table_{table_num}"] = result_employees

        month = date_col.split('.')[1]
        schedule[date_col] = {"month": month, "employees": employees_by_table}

    return schedule


def init_database(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS work_schedule (
                id SERIAL PRIMARY KEY,
                day VARCHAR(10) NOT NULL,
                month VARCHAR(2) NOT NULL,
                employees JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(day, month)
            )
        """)

        try:
            cur.execute("ALTER TABLE work_schedule ALTER COLUMN day TYPE VARCHAR(10)")
        except psycopg2.Error:
            pass
        conn.commit()


def save_to_database(conn, schedule):
    with conn.cursor() as cur:
        for day, data in schedule.items():
            month = data["month"]
            employees = data["employees"]

            cur.execute("""
                INSERT INTO work_schedule (day, month, employees)
                VALUES (%s, %s, %s)
                ON CONFLICT (day, month) DO UPDATE
                SET employees = EXCLUDED.employees,
                    created_at = CURRENT_TIMESTAMP
            """, (day, month, Json(employees)))
        conn.commit()


def main():
    if not DATABASE_URL:
        return

    if not SPREADSHEET_URL:
        return

    csv_url = get_csv_url(SPREADSHEET_URL)
    schedule = build_schedule(csv_url)

    try:
        conn = psycopg2.connect(DATABASE_URL)
    except Exception as e:
        return

    init_database(conn)

    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE work_schedule RESTART IDENTITY")
        conn.commit()

    save_to_database(conn, schedule)
    conn.close()


if __name__ == "__main__":
    main()