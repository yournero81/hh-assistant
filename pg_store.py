"""Small PostgreSQL adapter for the existing SQLite-backed domain store."""
import os
import re
from pathlib import Path
from contextlib import contextmanager

TABLES={
 'settings':('id','body'),'vacancies':('id','body','score','reason','letter','status','updated'),
 'tasks':('id','kind','ref','title','body','draft','status','created'),
 'events':('id','at','kind','detail'),'chats':('id','body','baseline'),
 'messages':('chat_id','id','body','status'),'outbox':('id','chat_id','text','status','created'),
 'hr_reviews':('task_id','body'),'model_reviews':('fingerprint','body'),'documents':('name','body')}
KEYS={'messages':('chat_id','id'),'hr_reviews':('task_id',),'model_reviews':('fingerprint',),'documents':('name',)}

class Row(dict):
    def __getitem__(self,key):
        return tuple(self.values())[key] if isinstance(key,int) else super().__getitem__(key)

class Result:
    def __init__(self, cursor):self.cursor=cursor
    def fetchone(self):
        row=self.cursor.fetchone();return Row(row) if row is not None else None
    def fetchall(self):return [Row(row) for row in self.cursor.fetchall()]
    def __iter__(self):return iter(self.fetchall())
    @property
    def rowcount(self):return self.cursor.rowcount

def translate(sql):
    sql=sql.strip().rstrip(';')
    sql=re.sub(r'\bINTEGER PRIMARY KEY\b','BIGSERIAL PRIMARY KEY',sql,flags=re.I)
    sql=re.sub(r'\bCAST\(([^)]+) AS INTEGER\)',r'CAST(\1 AS BIGINT)',sql,flags=re.I)
    ignore=bool(re.match(r'INSERT OR IGNORE\b',sql,re.I))
    replace=bool(re.match(r'INSERT OR REPLACE\b',sql,re.I))
    sql=re.sub(r'^INSERT OR (IGNORE|REPLACE)\b','INSERT',sql,flags=re.I)
    table=re.match(r'INSERT INTO\s+(\w+)',sql,re.I)
    if replace:
        name=table[1];columns=TABLES[name];keys=KEYS.get(name,('id',))
        sql=re.sub(r'^(INSERT INTO\s+\w+)\s+VALUES',r'\1 ('+','.join(columns)+') VALUES',sql,flags=re.I)
        sql+=' ON CONFLICT ('+','.join(keys)+') DO UPDATE SET '+','.join(c+'=EXCLUDED.'+c for c in columns if c not in keys)
    elif ignore:
        sql+=' ON CONFLICT DO NOTHING'
    # All app SQL uses positional placeholders; literal question marks never occur in queries.
    sql=sql.replace('?','%s')
    return sql

class Connection:
    def __init__(self, connection):self.connection=connection;self.last_id=None
    def execute(self,sql,params=()):
        if re.match(r'BEGIN IMMEDIATE',sql,re.I):
            return Result(self.connection.execute('SELECT pg_advisory_xact_lock(724736301)'))
        if re.match(r'SELECT last_insert_rowid\(\)',sql,re.I):
            return Result(self.connection.execute('SELECT %s AS id',(self.last_id,)))
        translated=translate(sql)
        auto=bool(re.match(r'INSERT INTO (tasks|events)\b',translated,re.I))
        if auto:translated+=' RETURNING id'
        cursor=self.connection.execute(translated,params)
        if auto:
            row=cursor.fetchone()
            if row:self.last_id=row['id']
        return Result(cursor)
    def executescript(self,script):
        for statement in script.split(';'):
            if statement.strip():self.execute(statement)

@contextmanager
def connect():
    import psycopg
    import certifi
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['HH_DATABASE_URL'],prepare_threshold=None,row_factory=dict_row,connect_timeout=15,sslmode='verify-full',sslrootcert=os.environ.get('HH_DB_CA_CERT_PATH',str(Path(__file__).resolve().parent/'supabase-ca.crt'))) as raw:
        schema=os.environ.get('HH_DB_SCHEMA','hh_assistant')
        if not re.fullmatch(r'hh_[a-z0-9_]+',schema):raise ValueError('Invalid database schema')
        raw.execute('SET LOCAL search_path TO '+schema)
        yield Connection(raw)

def initialize():
    import psycopg
    import certifi
    with psycopg.connect(os.environ['HH_DATABASE_URL'],prepare_threshold=None,connect_timeout=15,sslmode='verify-full',sslrootcert=os.environ.get('HH_DB_CA_CERT_PATH',str(Path(__file__).resolve().parent/'supabase-ca.crt'))) as raw:
        schema=os.environ.get('HH_DB_SCHEMA','hh_assistant')
        if not re.fullmatch(r'hh_[a-z0-9_]+',schema):raise ValueError('Invalid database schema')
        raw.execute('CREATE SCHEMA IF NOT EXISTS '+schema)
