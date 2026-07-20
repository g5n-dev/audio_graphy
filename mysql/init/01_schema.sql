# ============================================================
# AudioGraphy MySQL 8 init scripts
# Mounted at /docker-entrypoint-initdb.d/ — executed alphabetically
# on first container start (only when the data dir is empty).
# ============================================================

# --- 01_schema.sql : create databases, charset, time zone -------------
# The application database (MYSQL_DB) is auto-created by the MySQL image
# from the env var; we only need to provision the test database and
# timezone + grants here.

# Create a separate test database for pytest (auto-rollback fixtures)
CREATE DATABASE IF NOT EXISTS audiography_test
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON audiography_test.* TO 'audiography'@'%';

# Set session time zone for everyone — recordings carry recorded_at timestamps
# that must be Shanghai/CST consistent across container restarts.
SET GLOBAL time_zone = '+08:00';
SET SESSION time_zone = '+08:00';

# --- mysql_native_password is set via --default-authentication-plugin ---
# Make sure the application user uses it explicitly (avoids caching_sha2
# handshake quirks with older client libs).
ALTER USER 'audiography'@'%' IDENTIFIED WITH mysql_native_password
  BY 'change-me';

FLUSH PRIVILEGES;
