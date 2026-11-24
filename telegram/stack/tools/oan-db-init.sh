#!/bin/sh
# OAN Messaging database: owner role (migrations) and runtime role (least privilege), as
# in the SMS integration trial; then the Bot Services database.
set -eu
psql -v ON_ERROR_STOP=1 --username postgres <<SQL
CREATE ROLE oan_msg_owner LOGIN PASSWORD '${OAN_OWNER_PW}';
CREATE ROLE oan_msg_runtime LOGIN PASSWORD '${OAN_RUNTIME_PW}';
CREATE DATABASE oan_messaging OWNER oan_msg_owner;
SQL
psql -v ON_ERROR_STOP=1 --username postgres -d oan_messaging -c "ALTER SCHEMA public OWNER TO oan_msg_owner"
# Bot Services: its own database and owner role (migrations and runtime; Phase A).
psql -v ON_ERROR_STOP=1 --username postgres <<SQL
CREATE ROLE oan_bot LOGIN PASSWORD '${OAN_BOT_DB_PW}';
CREATE DATABASE oan_bot OWNER oan_bot;
SQL
psql -v ON_ERROR_STOP=1 --username postgres -d oan_bot -c "ALTER SCHEMA public OWNER TO oan_bot"
