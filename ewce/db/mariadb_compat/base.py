"""MySQL backend that also accepts MariaDB 10.4 (XAMPP's bundled version).

Django 5.1+ dropped MariaDB 10.4 by (a) a minimum-version gate and (b) removing the
10.5 check on INSERT ... RETURNING. We restore both behaviours exactly as Django 4.2 had them.
Switch DB_ENGINE to ``django.db.backends.mysql`` after upgrading to MariaDB 10.5+.
"""
import operator

from django.db.backends.mysql import base, features
from django.utils.functional import cached_property


class DatabaseFeatures(features.DatabaseFeatures):
    @property
    def minimum_database_version(self):
        if self.connection.mysql_is_mariadb:
            return (10, 4)
        return (8, 0, 11)

    @cached_property
    def can_return_columns_from_insert(self):
        return self.connection.mysql_is_mariadb and self.connection.mysql_version >= (10, 5, 0)

    can_return_rows_from_bulk_insert = property(operator.attrgetter("can_return_columns_from_insert"))


class DatabaseWrapper(base.DatabaseWrapper):
    features_class = DatabaseFeatures
