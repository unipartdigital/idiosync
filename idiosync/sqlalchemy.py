"""SQLAlchemy user database"""

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar, Mapping, Optional, Type
import uuid
import sqlalchemy
from sqlalchemy import create_engine, inspect, and_
from sqlalchemy.orm import sessionmaker, contains_eager
from sqlalchemy.types import TypeDecorator, BINARY, VARBINARY, Integer, String
from sqlalchemy.schema import MetaData
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.ext.associationproxy import ASSOCIATION_PROXY
import alembic
from .base import (Attribute, WritableEntry, WritableUser, WritableGroup,
                   Config, State, WritableDatabase)

NAMESPACE_SQL = uuid.UUID('b3c23456-05d8-4be5-b173-b57aeb30b4f4')

logger = logging.getLogger(__name__)

SqlOrm = Any


##############################################################################
#
# Reusable column types


class BinaryString(TypeDecorator):
    """Unicode string held in a binary column

    Apparently MySQL's support for Unicode has historically been so
    badly broken that applications such as MediaWiki have chosen to
    use raw binary columns and handle character encoding and decoding
    entirely at the application level.
    """

    impl = VARBINARY
    python_type = str

    def process_bind_param(self, value, dialect):
        """Encode Unicode string to raw column value"""
        if value is None:
            return value
        return value.encode('utf-8')

    def process_result_value(self, value, dialect):
        """Decode raw column value to Unicode string"""
        if value is None:
            return value
        return value.decode('utf-8')


class UnsignedInteger(TypeDecorator):
    """Unsigned integer column"""

    impl = Integer
    python_type = int

    def load_dialect_impl(self, dialect):
        """Get corresponding TypeEngine object"""
        if dialect.name == 'mysql':
            return dialect.type_descriptor(mysql.INTEGER(unsigned=True))
        return dialect.type_descriptor(Integer)

    def process_bind_param(self, value, dialect):
        """Encode unsigned integer to raw column value"""
        return value

    def process_result_value(self, value, dialect):
        """Decode raw column value to unsigned integer"""
        return value


class UuidBinary(TypeDecorator):
    """UUID column

    This implementation is loosely based on the "Backend-agnostic GUID
    type" recipe from the SQLAlchemy documentation.  For PostgreSQL,
    the native backend UUID type is used; for other databases a
    BINARY(16) is used.
    """

    impl = BINARY
    python_type = uuid.UUID

    def load_dialect_impl(self, dialect):
        """Get corresponding TypeEngine object"""
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(postgresql.UUID(as_uuid=True))
        return dialect.type_descriptor(BINARY(16))

    def process_bind_param(self, value, dialect):
        """Encode UUID object to raw column value"""
        if value is None or dialect.name == 'postgresql':
            return value
        return value.bytes

    def process_result_value(self, value, dialect):
        """Decode raw column value to UUID object"""
        if value is None or dialect.name == 'postgresql':
            return value
        return uuid.UUID(bytes=value)


class UuidChar(TypeDecorator):
    """UUID column

    This implementation is loosely based on the "Backend-agnostic GUID
    type" recipe from the SQLAlchemy documentation.  For PostgreSQL,
    the native backend UUID type is used; for other databases a
    CHAR(36) is used.

    Backends that erroneously choose to return bytes instead of
    strings are handled transparently.
    """

    impl = String
    python_type = uuid.UUID

    def load_dialect_impl(self, dialect):
        """Get corresponding TypeEngine object"""
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(postgresql.UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        """Encode UUID object to raw column value"""
        if value is None or dialect.name == 'postgresql':
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        """Decode raw column value to UUID object"""
        if value is None or dialect.name == 'postgresql':
            return value
        if isinstance(value, bytes):
            return uuid.UUID(value.decode())
        return uuid.UUID(value)


##############################################################################
#
# User database entries


@dataclass
class SqlModel:
    """A SQLAlchemy model"""

    orm: Type[SqlOrm]
    key: str
    syncid: Optional[str] = None
    member: Optional[str] = None


class SqlAttribute(Attribute):
    """A SQL user database attribute"""

    def __get__(self, instance, owner):
        """Get attribute value"""
        if instance is None:
            return self
        return getattr(instance.row, self.name)

    def __set__(self, instance, value):
        """Set attribute value"""
        setattr(instance.row, self.name, value)


class SqlEntryMeta(type):
    """SQL user database entry metaclass"""

    def __init__(cls, name, bases, dct):
        super().__init__(name, bases, dct)
        # Construct a namespace based on the table name if applicable
        if cls.model is not None:
            table = inspect(cls.model.orm).persist_selectable
            cls.uuid_ns = uuid.uuid5(NAMESPACE_SQL, table.name)


@dataclass
class SqlEntry(WritableEntry, metaclass=SqlEntryMeta):
    """A SQL user database entry"""

    row: SqlOrm

    model: ClassVar[SqlModel] = None
    """SQLAlchemy model for this table"""

    uuid_ns: ClassVar[uuid.UUID] = None
    """UUID namespace for entries within this table"""

    @property
    def key(self):
        """Canonical lookup key"""
        return getattr(self.row, self.model.key)

    @property
    def uuid(self):
        """Permanent identifier for this entry"""
        identity = inspect(self.row).identity
        return uuid.uuid5(self.uuid_ns, ':'.join(str(x) for x in identity))

    @property
    def syncid(self):
        """Synchronization identifier"""
        return getattr(self.row, self.model.syncid)

    @syncid.setter
    def syncid(self, value):
        """Synchronization identifier"""
        setattr(self.row, self.model.syncid, value)

    @classmethod
    def find(cls, key):
        """Look up user database entry"""
        query = cls.db.query(cls.model.orm)
        attr = getattr(cls.model.orm, cls.model.key)
        row = query.filter(attr == key).one_or_none()
        return cls(row) if row is not None else None

    @classmethod
    def query_syncid(cls, search):
        """Query user database by synchronization identifier"""
        query = cls.db.query(cls.model.orm)
        attr = getattr(cls.model.orm, cls.model.syncid)
        desc = inspect(cls.model.orm).all_orm_descriptors[cls.model.syncid]
        if desc.extension_type is ASSOCIATION_PROXY:
            # Use inner join and a direct filter on the proxied column
            # to improve query efficiency
            query = query.join(attr.local_attr).options(
                contains_eager(attr.local_attr)
            )
            attr = attr.remote_attr
        return query.filter(search(attr))

    @classmethod
    def find_syncid(cls, syncid):
        """Look up user database entry by synchronization identifier"""
        row = cls.query_syncid(lambda attr: attr == syncid).one_or_none()
        return cls(row) if row is not None else None

    @classmethod
    def find_syncids(cls, syncids, invert=False):
        """Look up user database entries by synchronization identifier"""
        query = cls.query_syncid(lambda attr: and_(
            attr.isnot(None),
            ~attr.in_(syncids) if invert else attr.in_(syncids),
        ))
        return (cls(row) for row in query)

    @classmethod
    def create(cls):
        """Create new user database entry"""
        row = cls.model.orm()
        cls.db.session.add(row)
        return cls(row)

    def delete(self):
        """Delete user database entry"""
        self.db.session.delete(self.row)

    @classmethod
    def prepare(cls):
        """Prepare for use as part of an idiosync user database"""
        # Create SyncId column if needed
        if cls.model is not None and cls.model.syncid is not None:
            attr = getattr(cls.model.orm, cls.model.syncid)
            desc = inspect(cls.model.orm).all_orm_descriptors[cls.model.syncid]
            if desc.extension_type is ASSOCIATION_PROXY:
                # Create remote table
                cls.db.prepare_table(attr.target_class)
            else:
                # Create column
                cls.db.prepare_column(attr)


class SqlUser(SqlEntry, WritableUser):
    """A SQL user database user"""

    @property
    def groups(self):
        """Groups of which this user is a member"""
        return (self.db.Group(x) for x in getattr(self.row, self.model.member))


class SqlGroup(SqlEntry, WritableGroup):
    """A SQL user database group"""

    @property
    def users(self):
        """Users who are members of this group"""
        return (self.db.User(x) for x in getattr(self.row, self.model.member))


##############################################################################
#
# User database synchronization identifiers


class SqlSyncId:
    """Synchronization identifier mixin"""

    __syncid__: ClassVar[str] = None

    def __init__(self, syncid: uuid.UUID = None, **kwargs) -> None:
        """Allow row to be constructed from a synchronization identifier"""
        if syncid is not None:
            kwargs[self.__syncid__] = syncid
        super().__init__(**kwargs)  # type: ignore[call-arg]


##############################################################################
#
# User database synchronization state


@dataclass
class SqlStateModel:
    """A SQLAlchemy synchronization state model"""

    orm: Type[SqlOrm]
    key: str
    value: str


@dataclass
class SqlState(State):
    """SQL user database synchronization state"""

    rows: Mapping[str, SqlOrm] = field(init=False, default_factory=dict)

    model: ClassVar[SqlStateModel] = None
    """SQLAlchemy synchronization state model"""

    def query(self, key):
        """Query database for synchronization state"""
        attr = getattr(self.model.orm, self.model.key)
        return self.db.query(self.model.orm).filter(attr == key)

    def __getitem__(self, key):
        if key in self.rows:
            row = self.rows[key]
        else:
            row = self.rows[key] = self.query(key).one_or_none()
        if row is None:
            raise KeyError
        return getattr(row, self.model.value)

    def __setitem__(self, key, value):
        if key in self.rows:
            row = self.rows[key]
        else:
            row = self.rows[key] = self.query(key).one_or_none()
        if row is None:
            row = self.rows[key] = self.model.orm(**{self.model.key: key})
            self.db.session.add(row)
        current = getattr(row, self.model.value)
        if value != current:
            setattr(row, self.model.value, value)

    def __delitem__(self, key):
        if key in self.rows:
            self.db.session.delete(self.rows[key])
            del self.rows[key]
        else:
            self.query(key).delete()

    def __iter__(self):
        return (x[0] for x in
                self.db.query(getattr(self.model.orm, self.model.key)))

    def __len__(self):
        return self.db.query(self.model.orm).count()

    def prepare(self):
        """Prepare for use as part of an idiosync user database"""
        self.db.prepare_table(self.model.orm)


##############################################################################
#
# User database


@dataclass
class SqlConfig(Config):
    """SQL user database configuration"""

    uri: str
    options: Mapping = field(default_factory=dict)


class SqlDatabase(WritableDatabase):
    """A SQL user database"""

    Config = SqlConfig
    User = SqlUser
    Group = SqlGroup
    State = SqlState

    config: SqlConfig
    engine: sqlalchemy.engine.Engine
    session: sqlalchemy.orm.Session
    _alembic: Optional[alembic.operations.Operations]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        echo = (logger.getEffectiveLevel() < logging.DEBUG)
        self.engine = create_engine(self.config.uri, echo=echo,
                                    **self.config.options)
        Session = sessionmaker(bind=self.engine)
        self.session = Session()
        self._alembic = None

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.config.uri)

    def query(self, *args, **kwargs):
        """Query database"""
        return self.session.query(*args, **kwargs)

    @property
    def users(self):
        """All users"""
        return (self.User(x) for x in self.query(self.User.model.orm))

    @property
    def groups(self):
        """All groups"""
        return (self.Group(x) for x in self.query(self.Group.model.orm))

    def commit(self):
        """Commit database changes"""
        self.session.commit()

    @property
    def alembic(self):
        """Alembic migration operations"""
        if self._alembic is None:
            conn = self.session.connection()
            ctx = alembic.migration.MigrationContext.configure(conn)
            self._alembic = alembic.operations.Operations(ctx)
        return self._alembic

    def prepare_table(self, orm):
        """Prepare table for use as part of an idiosync user database"""
        table = inspect(orm).persist_selectable
        if table.name not in inspect(self.engine).get_table_names():
            op = alembic.operations.ops.CreateTableOp.from_table(table)
            self.alembic.invoke(op)

    def prepare_column(self, column):
        """Prepare column for use as part of an idiosync user database"""
        # Use a temporary metadata in which the column is first
        # disassociated from the table, to work around an apparent bug
        # in alembic (or sqlalchemy) that would otherwise result in an
        # error "Column object 'c' already assigned to Table 't'".
        table = column.parent.persist_selectable.tometadata(MetaData())
        column = table.columns[column.name]
        columns = inspect(self.engine).get_columns(table.name)
        if not any(x['name'] == column.name for x in columns):
            op = alembic.operations.ops.AddColumnOp.from_column(column)
            column.table = None  # Workaround; see above
            self.alembic.invoke(op)
