-- 仅供本机演示，业务进程不使用 postgres 管理员连接。
CREATE ROLE aftercare_app LOGIN PASSWORD 'local-app-demo'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE aftercare_merchant LOGIN PASSWORD 'local-merchant-demo'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER ROLE aftercare_app SET timezone TO 'UTC';
ALTER ROLE aftercare_merchant SET timezone TO 'UTC';
