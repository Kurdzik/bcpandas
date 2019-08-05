# -*- coding: utf-8 -*-
"""
Created on Sat Aug  3 23:07:15 2019

@author: ydima
"""

import csv
import logging
import os

import pandas as pd
import pyodbc

from .constants import (
    DELIMITER,
    IF_EXISTS_OPTIONS,
    IN,
    NEWLINE,
    OUT,
    QUOTECHAR,
    SQL_TYPES,
    TABLE,
    VIEW,
)
from .utils import _get_sql_create_statement, bcp, build_format_file, get_temp_file, sqlcmd

# TODO add logging
logger = logging.getLogger(__name__)


class SqlCreds:
    """
    Credential object for all SQL operations.

    If `username` and `password` are not provided, `with_krb_auth` will be `True`.

    Parameters
    ----------
    server : str
    database : str
    username : str, optional
    password : str, optional

    Returns
    -------
    `bcpandas.SqlCreds`
    """

    def __init__(self, server, database, username=None, password=None):
        if not server or not database:
            raise ValueError(f"Server and database can't be None, you passed {server}, {database}")
        self.server = server
        self.database = database
        if username and password:
            self.username = username
            self.password = password
            self.with_krb_auth = False
        else:
            self.with_krb_auth = True
        logger.info(f"Created creds:\t{self}")

    def __repr__(self):
        # adopted from https://github.com/erdewit/ib_insync/blob/master/ib_insync/objects.py#L51
        clsName = self.__class__.__qualname__
        kwargs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items() if k != "password")
        if hasattr(self, "password"):
            kwargs += ", password=[REDACTED]"
        return f"{clsName}({kwargs})"

    __str__ = __repr__


def to_sql(
    df,
    table_name,
    creds,
    sql_type="table",
    schema="dbo",
    index=True,
    if_exists="fail",
    batch_size=None,
    debug=False,
):
    """
    Writes the pandas DataFrame to a SQL table or view.

    Will write all columns to the table or view. 
    Assumes the SQL table or view has the same number, name, and type of columns.
    To only write parts of the DataFrame, filter it beforehand and pass that to this function.

    Parameters
    ----------
    df : pandas.DataFrame
    table_name : str
        Name of SQL table or view.
    creds : bcpandas.SqlCreds
        The credentials used in the SQL database.
    sql_type : {'table', 'view'}, default 'table'
        The type of SQL object of the destination.
    schema : str, default 'dbo'
        The SQL schema.
    index : bool, default True
        Write DataFrame index as a column. Uses the index name as the column
        name in the table.
    if_exists : {'fail', 'replace', 'append'}, default 'fail'
        How to behave if the table already exists.
        * fail: Raise a ValueError.
        * replace: Drop the table before inserting new values.
        * append: Insert new values to the existing table.
    batch_size : int, optional
        Rows will be written in batches of this size at a time. By default,
        all rows will be written at once.
    debug : bool, default False
        If True, will not delete the temporary CSV and format files, and will output their location.
    """
    # validation
    assert sql_type in SQL_TYPES
    assert if_exists in IF_EXISTS_OPTIONS

    # save to temp path
    csv_file_path = get_temp_file()
    df.to_csv(
        path_or_buf=csv_file_path,
        sep=DELIMITER,
        header=False,
        index=False,
        quoting=csv.QUOTE_MINIMAL,  # pandas default
        quotechar=QUOTECHAR,
        line_terminator=NEWLINE,
        doublequote=True,
        escapechar=None,  # not needed, as using doublequote
    )
    logger.debug(f"Saved dataframe to temp CSV file at {csv_file_path}")

    # build format file
    fmt_file_path = get_temp_file()
    fmt_file_txt = build_format_file(df=df)
    with open(fmt_file_path, "w") as ff:
        ff.write(fmt_file_txt)
    logger.debug(f"Created BCP format file at {fmt_file_path}")

    try:
        if if_exists == "fail":
            # TODO check if db table/view exists, raise ValueError if exists
            raise NotImplementedError()
        elif if_exists == "replace":
            # TODO fix
            sqlcmd(
                command=_get_sql_create_statement(df=df, table_name=table_name, schema=schema),
                server=creds.server,
                database=creds.database,
                username=creds.username,
                password=creds.password,
            )
        elif if_exists == "append":
            pass  # don't need to do anything

        # either way, BCP data in
        bcp(
            sql_item=table_name,
            direction=IN,
            flat_file=csv_file_path,
            format_file_path=fmt_file_path,
            creds=creds,
            sql_type=sql_type,
            schema=schema,
            batch_size=batch_size,
        )
    finally:
        if not debug:
            logger.debug(f"Deleting temp CSV and format files")
            os.remove(csv_file_path)
            os.remove(fmt_file_path)
        else:
            logger.debug(
                f"`to_sql` DEBUG mode, not deleting the files. CSV file is at {csv_file_path}, format file is at {fmt_file_path}"
            )


def read_sql(
    table_name,
    creds,
    sql_type="table",
    schema="dbo",
    mssql_odbc_driver_version=17,
    batch_size=10000,
):
    # check params
    assert sql_type in SQL_TYPES
    assert mssql_odbc_driver_version in {13, 17}, "SQL Server ODBC Driver must be either 13 or 17"

    # set up objects
    if ";" in table_name:
        raise ValueError(
            "The SQL item cannot contain the ';' character, it interferes with getting the column names"
        )

    # TODO not sure how to support Kerberos here
    db_conn = pyodbc.connect(
        f"DRIVER={{ODBC Driver {mssql_odbc_driver_version} for SQL Server}};SERVER={creds.server};"
        f"DATABASE={creds.database};UID={creds.username};PWD={creds.password}"
    )

    # read top 2 rows of query to get the columns
    _from_clause = table_name if sql_type in (TABLE, VIEW) else f"({table_name})"
    cols = pd.read_sql_query(sql=f"SELECT TOP 2 * FROM {_from_clause} as qry", con=db_conn).columns

    file_path = get_temp_file()
    try:
        bcp(
            sql_item=table_name,
            direction=OUT,
            flat_file=file_path,
            creds=creds,
            sql_type=sql_type,
            schema=schema,
            batch_size=batch_size,
        )
        return pd.read_csv(filepath_or_buffer=file_path, header=None, names=cols, index_col=False)
    finally:
        os.remove(file_path)
