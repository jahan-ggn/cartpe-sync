"""Database connection management with connection pooling"""

import logging
from contextlib import contextmanager

from mysql.connector import Error, pooling

from config.settings import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages MySQL database connections with connection pooling"""

    _pool = None

    @classmethod
    def initialize_pool(cls):
        """Initialize the connection pool"""
        if cls._pool is None:
            try:
                cls._pool = pooling.MySQLConnectionPool(
                    pool_name=settings.POOL_NAME,
                    pool_size=settings.POOL_SIZE,
                    pool_reset_session=True,
                    host=settings.DB_HOST,
                    port=settings.DB_PORT,
                    database=settings.DB_NAME,
                    user=settings.DB_USER,
                    password=settings.DB_PASSWORD,
                    charset="utf8mb4",
                    autocommit=False,
                    time_zone="+05:30",
                )
                logger.info(
                    f"Database pool initialized with {settings.POOL_SIZE} connections"
                )
            except Error as e:
                logger.error(f"Error creating connection pool: {e}")
                raise

    @staticmethod
    def _rollback_quietly(connection) -> None:
        """Best-effort rollback; log but never mask the original exception"""
        try:
            connection.rollback()
        except Exception:
            logger.warning("Rollback failed", exc_info=True)

    @classmethod
    @contextmanager
    def get_connection(cls):
        """Context manager for database connections"""
        if cls._pool is None:
            cls.initialize_pool()

        connection = None
        try:
            connection = cls._pool.get_connection()
            yield connection
            connection.commit()
        except Error as e:
            if connection:
                cls._rollback_quietly(connection)
            logger.error(f"Database error: {e}")
            raise
        except Exception:
            if connection:
                cls._rollback_quietly(connection)
            raise
        finally:
            if connection and connection.is_connected():
                connection.close()

    @classmethod
    def execute_query(cls, query, params=None, fetch=False):
        """Execute a single query with optional parameters"""
        with cls.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            try:
                cursor.execute(query, params or ())
                return cursor.fetchall() if fetch else cursor.rowcount
            finally:
                cursor.close()

    @classmethod
    def execute_many(cls, query, data):
        """Execute batch insert/update operations"""
        with cls.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.executemany(query, data)
                return cursor.rowcount
            finally:
                cursor.close()
