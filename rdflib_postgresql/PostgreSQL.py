## Copyright (c) 2009, Intel Corporation. All rights reserved.

## Redistribution and use in source and binary forms, with or without
## modification, are permitted provided that the following conditions are
## met:

##   * Redistributions of source code must retain the above copyright
## notice, this list of conditions and the following disclaimer.

##   * Redistributions in binary form must reproduce the above
## copyright notice, this list of conditions and the following
## disclaimer in the documentation and/or other materials provided
## with the distribution.

##   * Neither the name of Daniel Krech nor the names of its
## contributors may be used to endorse or promote products derived
## from this software without specific prior written permission.

## THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
## "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
## LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
## A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
## OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
## SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
## LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
## DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
## THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
## (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
## OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

try:
    import psycopg2
    has_psycopg2 = True
except ImportError:
    has_psycopg2 = False
import sys
import itertools
import random
import string
from rdflib.graph import Graph, QuotedGraph
from rdflib import Literal, RDF, URIRef
from rdfextras.store.REGEXMatching import NATIVE_REGEX, REGEXTerm
from rdfextras.store.AbstractSQLStore import (
    COUNT_SELECT,
    CONTEXT_SELECT,
    TRIPLE_SELECT,
    ASSERTED_NON_TYPE_PARTITION,
    ASSERTED_TYPE_PARTITION,
    QUOTED_PARTITION,
    ASSERTED_LITERAL_PARTITION,
    FULL_TRIPLE_PARTITIONS,
    table_name_prefixes,
    AbstractSQLStore,
    extractTriple,
    )
from rdflib.py3compat import PY3
from rdflib.store import NO_STORE, VALID_STORE
import logging


def bb(u):
    return u.encode('utf-8')

Any = None


def _debug(*args, **kw):
    logger = logging.getLogger(__name__)
    logger.debug(*args, **kw)


def ParseConfigurationString(config_string):
    """
    Parses a configuration string in the form:
    key1=val1 key2=val2 key3=val3 ...
    The following configuration keys are expected (not all are required):
    user
    password
    dbname
    host
    port (optional - defaults to 5432)
    """
    parts = config_string.split(' ')
    parts = (part.split('=', 1) for part in parts)
    parts = ((k.strip(), v.strip()) for (k, v) in parts)
    kvDict = dict(parts)

    for requiredKey in ('user', 'dbname'):
        assert requiredKey in kvDict, (requiredKey, kvDict)
    if 'port' in kvDict:
        try:
            kvDict['port'] = int(kvDict['port'])
        except:
            raise RuntimeError('PostgreSQL port must be a valid integer')
    else:
        kvDict['port'] = 5432
    kvDict.setdefault('password', '')
    return kvDict


def GetConfigurationString(configuration):
    """
    Given a config-form string, return a dsn-form string
    """
    configDict = ParseConfigurationString(configuration)

    dsn = dict(dbname=configDict['dbname'],
               user=configDict['user'],
               password=configDict['password'])

    for name in ('host', 'port', 'sslmode'):
        if name in configDict:
            dsn[name] = configDict.get(name)

    dsn = ('%s=%s' % item for item in dsn.iteritems())
    return ' '.join(dsn)


# Though I appreciate that this was made into a function rather than
# a method since it was universal, sadly different DBs quote values
# differently. So I have to pull this, and all methods which call it,
# into the Postgres implementation level.

def unionSELECT(selectComponents, distinct=False, selectType=TRIPLE_SELECT):
    """
    Helper function for building union all select statement
    Takes a list of:
    - table name
    - table alias
    - table type (literal, type, asserted, quoted)
    - where clause string
    """
    selects = []
    for tableName, tableAlias, whereClause, tableType in selectComponents:

        if selectType == COUNT_SELECT:
            selectString = "select count(*)"
            tableSource = " from %s " % tableName
        elif selectType == CONTEXT_SELECT:
            selectString = "select %s.context" % tableAlias
            tableSource = " from %s as %s " % (tableName, tableAlias)
        elif tableType in FULL_TRIPLE_PARTITIONS:
            selectString = "select *"
            tableSource = " from %s as %s " % (tableName, tableAlias)
        elif tableType == ASSERTED_TYPE_PARTITION:
            selectString = \
                """select %s.member as subject,""" % tableAlias + \
                """'%s' as predicate,""" % RDF.type + \
                """%s.klass as object,""" % tableAlias + \
                """%s.context as context,""" % tableAlias + \
                """%s.termComb as termComb,""" % tableAlias + \
                """NULL as objLanguage, NULL as objDatatype"""
            tableSource = " from %s as %s " % (tableName, tableAlias)
        elif tableType == ASSERTED_NON_TYPE_PARTITION:
            selectString =\
                """select *, NULL as objLanguage, NULL as objDatatype"""
            tableSource = " from %s as %s " % (tableName, tableAlias)

        selects.append(selectString + tableSource + whereClause)

    orderStmt = ''
    if selectType == TRIPLE_SELECT:
        orderStmt = ' order by subject, predicate, object'
    if distinct:
        return ' union '.join(selects) + orderStmt
    else:
        return ' union all '.join(selects) + orderStmt


class PostgreSQL(AbstractSQLStore):
    """
    PostgreSQL store formula-aware implementation.  It stores its triples in
    the following partitions, as per AbstractSQLStore:

    * Asserted non rdf:type statements
    * Asserted rdf:type statements (in a table which models Class
        membership) - The motivation for this partition is primarily
        query speed and scalability as most graphs will always have more
        rdf:type statements than others
    * All Quoted statements

    In addition it persists namespace mappings in a seperate table
    """
    context_aware = True
    formula_aware = True
    transaction_aware = True
    regex_matching = NATIVE_REGEX
    autocommit_default = False
    # _Store__node_pickler = None

    def __init__(self, configuration=None, identifier=None):
        if not has_psycopg2:
            raise ImportError("Unable to import psycopg2, store is unusable.")
        self.__open = False
        self._Store__node_pickler = None
        super(PostgreSQL, self).__init__(
                configuration=configuration, identifier=identifier)

    def open(self, configuration, create=True):
        """
        Opens the store specified by the configuration string. If
        create is True a store will be created if it does not already
        exist. If create is False and a store does not already exist
        an exception is raised. An exception is also raised if a store
        exists, but there is insufficient permissions to open the
        store.
        """
        # sys.stderr.write("Entering 'open'\n")
        self._db = psycopg2.connect(GetConfigurationString(configuration))
        self.configuration = configuration
        if self._db:
            if create:
                #sys.stderr.write("Calling init_db\n")
                self.init_db(configuration=configuration)

            if self.db_exists(configuration=configuration):
                #sys.stderr.write("Returning VALID_STORE\n")
                return VALID_STORE
            else:
                self._db = None
                #sys.stderr.write("Returning NO_STORE\n")
                return NO_STORE
        else:
            #sys.stderr.write("Returning NO_STORE\n")
            return NO_STORE
        #sys.stderr.write("'open' returning\n")

    def db_exists(self, configuration=None):
        #sys.stderr.write("Entering 'db_exists'\n")
        if not self._db:
            self._db = psycopg2.connect(GetConfigurationString(configuration))
        c = self._db.cursor()
        c.execute("SELECT relname from pg_class")
        tbls = [rt[0] for rt in c.fetchall()]
        c.close()
        for tn in [tbl % (self._internedId) for tbl in table_name_prefixes]:
            if tn not in tbls:
                # sys.stderr.write("table %s Doesn't exist\n" % (tn))
                return 0
        return 1

    def init_db(self, configuration=None):
        # sys.stderr.write("Entering 'init_db'\n")
        if not self.db_exists(configuration=configuration):
            # sys.stderr.write("not db_exists, creating tables'\n")
            c = self._db.cursor()
            for x in CREATE_TABLE_STMTS:
                c.execute(x % (self._internedId))
            for x in ['asserted_statements', 'literal_statements',
                      'quoted_statements', 'type_statements',
                      'namespace_binds']:
                c.execute(
                    """COMMENT ON TABLE "{}_{}" IS 'identifier: {}';""".format(
                    self._internedId, x, self.identifier))
            for tblName, indices in INDICES:
                for indexName, columns in indices:
                    c.execute("CREATE INDEX %s on %s (%s)" % (
                        (indexName % self._internedId),
                        (tblName % self._internedId),
                        ', '.join(columns)))
        else:
            # sys.stderr.write(
            #    "is 'db_exists, deleting records from tables'\n")
            c = self._db.cursor()
            for tblname in table_name_prefixes:
                fullname = tblname % self._internedId
                try:
                    c.execute("DELETE FROM %s;" % fullname)
                    # sys.stderr.write("Table: %s cleared\n" % (fullname))
                except Exception, errmsg:
                    sys.stderr.write(
                        "unable to clear table: %s (%s)\n" % (
                        fullname, errmsg))
        c.close()
        self._db.commit()

    def destroy(self, configuration):
        """
        Opposite of init_db, takes a config string
        """
        # sys.stderr.write("Entering 'destroy'\n")
        self.init_db(configuration=configuration)
        # sys.stderr.write("Connecting to db %s\n" % (configuration))
        db = psycopg2.connect(GetConfigurationString(configuration))
        # sys.stderr.write("Opening cursor\n")
        c = db.cursor()
        # sys.stderr.write("Dropping tables\n")
        for tblname in table_name_prefixes:
            fullname = tblname % self._internedId
            # sys.stderr.write("Dropping table %s\n" % fullname)
            try:
                c.execute("DROP TABLE IF EXISTS %s CASCADE" % fullname)
                # sys.stderr.write("Table: %s dropped\n" % (fullname))
            except Exception, errmsg:
                sys.stderr.write(
                  "unable to drop table: %s (%s)\n" % (fullname, errmsg))
                # _debug(
                #   "unable to drop table: %s (%s)" % (fullname, errmsg))
        # sys.stderr.write("Dropping indices\n")
        for tblName, indices in INDICES:
            for indexName, columns in indices:
                # _debug(
                #  "Dropping index %s\n" % (indexName % self._internedId))
                try:
                    c.execute("DROP INDEX IF EXISTS %s CASCADE" % (
                                        (indexName % self._internedId)))
                except Exception, errmsg:
                    sys.stderr.write(
                      "unable to drop index: %s\n" % (
                              indexName % self._internedId))
                #     _debug(
                #           "unable to drop index: %s" % (
                #                   indexName % self._internedId))
        # _debug("calling db_commit\n")
        db.commit()
        # _debug("calling c.close'\n")
        c.close()
        # _debug("calling db.close'\n")
        db.close()
        # _debug("Leaving 'destroy'\n")

        # _debug("Destroyed Close World Universe %s in PostgreSQL database %s",
        #        self.identifier, configuration)

    def EscapeQuotes(self, qstr):
        """
        Overridden because executeSQL uses PostgreSQL's dollar-quoted strings
        """
        if qstr is None:
            return ''
        return qstr

    # copied and pasted primarily to use the local unionSELECT instead
    # of the one provided by AbstractSQLStore
    def triples(self, (subject, predicate, obj), context=None):
        """
        A generator over all the triples matching pattern. Pattern can
        be any objects for comparing against nodes in the store, for
        example, RegExLiteral, Date? DateRange?

        .. sourcecode:: text

            quoted table:                <id>_quoted_statements
            asserted rdf:type table:     <id>_type_statements
            asserted non rdf:type table: <id>_asserted_statements

            triple columns: subject, predicate, object, context, termComb,
                            objLanguage, objDatatype
            class membership columns: member, klass, context termComb

        FIXME:  These union all selects *may* be further optimized by joins

        """
        quoted_table = "%s_quoted_statements" % self._internedId
        asserted_table = "%s_asserted_statements" % self._internedId
        asserted_type_table = "%s_type_statements" % self._internedId
        literal_table = "%s_literal_statements" % self._internedId
        c = self._db.cursor()

        parameters = []

        if predicate == RDF.type:
            # select from asserted rdf:type partition and quoted table (if a
            # context is specified)
            clauseString, params = self.buildClause(
                'typeTable', subject, RDF.type, obj, context, True)
            parameters.extend(params)
            selects = [
                (
                    asserted_type_table,
                    'typeTable',
                    clauseString,
                    ASSERTED_TYPE_PARTITION
                ),
            ]

        elif isinstance(predicate, REGEXTerm) \
                and predicate.compiledExpr.match(RDF.type) \
                or not predicate:
            # Select from quoted partition (if context is specified), literal
            # partition if (obj is Literal or None) and asserted non rdf:type
            # partition (if obj is URIRef or None)
            selects = []
            if not self.STRONGLY_TYPED_TERMS \
                    or isinstance(obj, Literal) \
                    or not obj \
                    or (self.STRONGLY_TYPED_TERMS
                        and isinstance(obj, REGEXTerm)):
                clauseString, params = self.buildClause(
                    'literal', subject, predicate, obj, context)
                parameters.extend(params)
                selects.append((
                               literal_table,
                               'literal',
                               clauseString,
                               ASSERTED_LITERAL_PARTITION
                               ))
            if not isinstance(obj, Literal) \
                    and not (isinstance(obj, REGEXTerm)
                             and self.STRONGLY_TYPED_TERMS) \
                    or not obj:
                clauseString, params = self.buildClause(
                    'asserted', subject, predicate, obj, context)
                parameters.extend(params)
                selects.append((
                               asserted_table,
                               'asserted',
                               clauseString,
                               ASSERTED_NON_TYPE_PARTITION
                               ))

            clauseString, params = self.buildClause(
                'typeTable', subject, RDF.type, obj, context, True)
            parameters.extend(params)
            selects.append(
                (
                    asserted_type_table,
                    'typeTable',
                    clauseString,
                    ASSERTED_TYPE_PARTITION
                )
            )

        elif predicate:
            # select from asserted non rdf:type partition (optionally), quoted
            # partition (if context is speciied), and literal partition
            # (optionally)
            selects = []
            if not self.STRONGLY_TYPED_TERMS \
                    or isinstance(obj, Literal) \
                    or not obj \
                    or (self.STRONGLY_TYPED_TERMS
                        and isinstance(obj, REGEXTerm)):
                clauseString, params = self.buildClause(
                    'literal', subject, predicate, obj, context)
                parameters.extend(params)
                selects.append((
                               literal_table,
                               'literal',
                               clauseString,
                               ASSERTED_LITERAL_PARTITION
                               ))
            if not isinstance(obj, Literal) \
                    and not (isinstance(obj, REGEXTerm)
                             and self.STRONGLY_TYPED_TERMS) \
                    or not obj:
                clauseString, params = self.buildClause(
                    'asserted', subject, predicate, obj, context)
                parameters.extend(params)
                selects.append((
                               asserted_table,
                               'asserted',
                               clauseString,
                               ASSERTED_NON_TYPE_PARTITION
                               ))

        if context is not None:
            clauseString, params = self.buildClause(
                'quoted', subject, predicate, obj, context)
            parameters.extend(params)
            selects.append(
                (
                    quoted_table,
                    'quoted',
                    clauseString,
                    QUOTED_PARTITION
                )
            )

        q = self._normalizeSQLCmd(unionSELECT(selects))
        self.executeSQL(c, q, parameters)
        rt = c.fetchone()
        while rt:
            s, p, o, (graphKlass, idKlass, graphId) = \
                extractTriple(rt, self, context)
            currentContext = graphKlass(self, idKlass(graphId))
            contexts = [currentContext]
            rt = next = c.fetchone()
            sameTriple = next and \
                extractTriple(next, self, context)[:3] == (s, p, o)
            while sameTriple:
                s2, p2, o2, (graphKlass, idKlass, graphId) = \
                    extractTriple(next, self, context)
                c2 = graphKlass(self, idKlass(graphId))
                contexts.append(c2)
                rt = next = c.fetchone()
                sameTriple = next and \
                    extractTriple(next, self, context)[:3] == (s, p, o)
            yield (s, p, o), (c for c in contexts)

    def __repr__(self):
        """
        Copied and pasted primarily to use the local unionSELECT instead
        of the one provided by AbstractSQLStore
        """
        try:
            c = self._db.cursor()
        except AttributeError:
            return "<Parititioned PostgreSQL N3 Store>"
        quoted_table = "%s_quoted_statements" % self._internedId
        asserted_table = "%s_asserted_statements" % self._internedId
        asserted_type_table = "%s_type_statements" % self._internedId
        literal_table = "%s_literal_statements" % self._internedId

        selects = [
            (
                asserted_type_table,
                'typeTable',
                '',
                ASSERTED_TYPE_PARTITION
            ),
            (
                quoted_table,
                'quoted',
                '',
                QUOTED_PARTITION
            ),
            (
                asserted_table,
                'asserted',
                '',
                ASSERTED_NON_TYPE_PARTITION
            ),
            (
                literal_table,
                'literal',
                '',
                ASSERTED_LITERAL_PARTITION
            ),
        ]
        q = unionSELECT(selects, distinct=False, selectType=COUNT_SELECT)
        self.executeSQL(c, self._normalizeSQLCmd(q))
        rt = c.fetchall()
        typeLen, quotedLen, assertedLen, literalLen = \
            [rtTuple[0] for rtTuple in rt]

        # return "<Partitioned PostgreSQL N3 Store: %s contexts, %s " + \
        #        "classification assertions, %s quoted statements, %s " + \
        #        "property/value assertions, and %s other assertions>" % \
        #                 (len([c for c in self.contexts()]),
        #                  typeLen, quotedLen, literalLen, assertedLen)

        return "<Parititioned PostgreSQL N3 Store>"

    def __len__(self, context=None):
        """
        Number of statements in the store.
        Copied and pasted primarily to use the local unionSELECT instead
        of the one provided by AbstractSQLStore
        """
        c = self._db.cursor()
        quoted_table = "%s_quoted_statements" % self._internedId
        asserted_table = "%s_asserted_statements" % self._internedId
        asserted_type_table = "%s_type_statements" % self._internedId
        literal_table = "%s_literal_statements" % self._internedId

        parameters = []
        quotedContext = assertedContext = typeContext = literalContext = None

        clauseParts = self.buildContextClause(context, quoted_table)
        if clauseParts:
            quotedContext, params = clauseParts
            parameters.extend([p for p in params if p])

        clauseParts = self.buildContextClause(context, asserted_table)
        if clauseParts:
            assertedContext, params = clauseParts
            parameters.extend([p for p in params if p])

        clauseParts = self.buildContextClause(context, asserted_type_table)
        if clauseParts:
            typeContext, params = clauseParts
            parameters.extend([p for p in params if p])

        clauseParts = self.buildContextClause(context, literal_table)
        if clauseParts:
            literalContext, params = clauseParts
            parameters.extend([p for p in params if p])

        if context is not None:
            selects = [
                (
                    asserted_type_table,
                    'typeTable',
                    typeContext and 'where ' + typeContext or '',
                    ASSERTED_TYPE_PARTITION
                ),
                (
                    quoted_table,
                    'quoted',
                    quotedContext and 'where ' + quotedContext or '',
                    QUOTED_PARTITION
                ),
                (
                    asserted_table,
                    'asserted',
                    assertedContext and 'where ' + assertedContext or '',
                    ASSERTED_NON_TYPE_PARTITION
                ),
                (
                    literal_table,
                    'literal',
                    literalContext and 'where ' + literalContext or '',
                    ASSERTED_LITERAL_PARTITION
                ),
            ]
            q = unionSELECT(selects, distinct=True, selectType=COUNT_SELECT)
        else:
            selects = [
                (
                    asserted_type_table,
                    'typeTable',
                    typeContext and 'where ' + typeContext or '',
                    ASSERTED_TYPE_PARTITION
                ),
                (
                    asserted_table,
                    'asserted',
                    assertedContext and 'where ' + assertedContext or '',
                    ASSERTED_NON_TYPE_PARTITION
                ),
                (
                    literal_table,
                    'literal',
                    literalContext and 'where ' + literalContext or '',
                    ASSERTED_LITERAL_PARTITION
                ),
            ]
            q = unionSELECT(selects, distinct=False, selectType=COUNT_SELECT)

        # sys.stderr.write("__len__")

        self.executeSQL(c, self._normalizeSQLCmd(q), parameters)
        rt = c.fetchall()
        c.close()
        # sys.stderr.write("\n%s\n" % str([r[0] for r in rt]))
        return reduce(lambda x, y: x + y, [rtTuple[0] for rtTuple in rt])

    def contexts(self, triple=None):
        """
        This is taken from AbstractSQLStore, and modified, specifically
        to not query quoted_statements. The comments in the original
        indicate that quoted_statements were queried conditionally, but
        the code does otherwise.

        As far as i can tell, quoted_statements contains formulae, which
        should not be returned as valid global contexts (at least, as per
        the in-memory and MySQL store implementations), so those queries
        have been completely excised until a case is made that they are
        necessary.

        It's reasonable that the AbstractSQLStore implementation is closer
        to the original design, but this conforms to working implementations.
        """
        c = self._db.cursor()
        asserted_table = "%s_asserted_statements" % self._internedId
        asserted_type_table = "%s_type_statements" % self._internedId
        literal_table = "%s_literal_statements" % self._internedId

        parameters = []

        if triple is not None:
            subject, predicate, obj = triple
            if predicate == RDF.type:
                # select from asserted rdf:type partition
                clauseString, params = self.buildClause(
                    'typeTable', subject, RDF.type, obj, Any, True)
                parameters.extend(params)
                selects = [
                    (
                        asserted_type_table,
                        'typeTable',
                      clauseString,
                      ASSERTED_TYPE_PARTITION
                    ),
                ]

            elif isinstance(predicate, REGEXTerm) and \
                    predicate.compiledExpr.match(RDF.type) or not predicate:
                # Select from literal partition if (obj is Literal or None)
                # and asserted non rdf:type partition (if obj is URIRef or
                # None)
                clauseString, params = self.buildClause(
                        'typeTable', subject, RDF.type, obj, Any, True)
                parameters.extend(params)
                selects = [
                    (
                      asserted_type_table,
                      'typeTable',
                      clauseString,
                      ASSERTED_TYPE_PARTITION
                    ),
                ]

                if not self.STRONGLY_TYPED_TERMS or isinstance(obj, Literal) \
                        or not obj \
                        or (self.STRONGLY_TYPED_TERMS and
                            isinstance(obj, REGEXTerm)):
                    clauseString, params = self.buildClause(
                        'literal', subject, predicate, obj)
                    parameters.extend(params)
                    selects.append((
                      literal_table,
                      'literal',
                      clauseString,
                      ASSERTED_LITERAL_PARTITION
                    ))
                if not isinstance(obj, Literal) \
                        and not (isinstance(obj, REGEXTerm)
                        and self.STRONGLY_TYPED_TERMS) or not obj:
                    clauseString, params = self.buildClause(
                                'asserted', subject, predicate, obj)
                    parameters.extend(params)
                    selects.append((
                      asserted_table,
                      'asserted',
                      clauseString,
                      ASSERTED_NON_TYPE_PARTITION
                    ))

            elif predicate:
                # select from asserted non rdf:type partition (optionally)
                # and literal partition (optionally)
                selects = []
                if not self.STRONGLY_TYPED_TERMS or isinstance(obj, Literal) \
                        or not obj \
                        or (self.STRONGLY_TYPED_TERMS
                            and isinstance(obj, REGEXTerm)):
                    clauseString, params = self.buildClause(
                                        'literal', subject, predicate, obj)
                    parameters.extend(params)
                    selects.append((
                      literal_table,
                      'literal',
                      clauseString,
                      ASSERTED_LITERAL_PARTITION
                    ))
                if not isinstance(obj, Literal) \
                        and not (isinstance(obj, REGEXTerm)
                        and self.STRONGLY_TYPED_TERMS) \
                        or not obj:
                    clauseString, params = self.buildClause(
                                        'asserted', subject, predicate, obj)
                    parameters.extend(params)
                    selects.append((
                      asserted_table,
                      'asserted',
                      clauseString,
                      ASSERTED_NON_TYPE_PARTITION
                ))

            q = unionSELECT(selects, distinct=True, selectType=CONTEXT_SELECT)
        else:
            selects = [
                (
                  asserted_type_table,
                  'typeTable',
                  '',
                  ASSERTED_TYPE_PARTITION
                ),
                (
                  asserted_table,
                  'asserted',
                  '',
                  ASSERTED_NON_TYPE_PARTITION
                ),
                (
                  literal_table,
                  'literal',
                  '',
                  ASSERTED_LITERAL_PARTITION
                ),
            ]
            q = unionSELECT(selects, distinct=True, selectType=CONTEXT_SELECT)

        self.executeSQL(c, self._normalizeSQLCmd(q), parameters)
        rt = c.fetchall()
        for contextId in [x[0] for x in rt]:
            yield Graph(self, URIRef(contextId))
        c.close()

    # overridden for quote-character reasons
    def executeSQL(self, cursor, qStr, params=None, paramList=False):
        """
        This takes the query string and parameters and (depending on the SQL
        implementation) either fill in the parameter in-place or pass it on to
        the Python DB impl (if it supports this). The default (here) is to
        fill the parameters in-place surrounding each param with quote
        characters
        """
        def prepitem(item):
            if isinstance(item, int) or item == 'NULL':
                return item
            else:
                tag = [random.choice(string.ascii_lowercase)
                            for dummy in itertools.repeat(None, 5)]
                tag = '$%s$' % ''.join(tag)
                try:
                    return u"%s%s%s" % (tag, item.decode('utf-8'), tag)
                except:
                    return u"%s%s%s" % (tag, item, tag)
        if not params:
            # sys.stderr.write("\n%s\n" % qStr)
            cursor.execute(unicode(qStr))
        elif paramList:
            raise Exception("Not supported!")
        else:
            params = tuple([prepitem(item) for item in params])
            querystr = unicode(qStr).replace('"', "'")
            qs = querystr % params
            # sys.stderr.write("\n%s\n" % qs)
            cursor.execute(qs)

    def buildGenericClause(self, generic, value, tableName):
        """
        New method abstracting much cut/paste code from AbstractSQLStore.
        """
        if isinstance(value, REGEXTerm):
            return " REGEXP (%s, " + " %s)" % (tableName and '%s.%s' %
                                (tableName, generic) or generic), [value]
        elif isinstance(value, list):
            clauseStrings = []
            paramStrings = []
            for s in value:
                if isinstance(s, REGEXTerm):
                    clauseStrings.append(" REGEXP (%s, " + " %s)" %
                            (tableName and '%s.%s' %
                                (tableName, generic) or generic) + " %s")
                    paramStrings.append(self.normalizeTerm(s))
                elif isinstance(s, (QuotedGraph, Graph)):
                    clauseStrings.append("%s=" % (tableName and '%s.%s' %
                                    (tableName, generic) or generic) + "%s")
                    paramStrings.append(self.normalizeTerm(s.identifier))
                else:
                    clauseStrings.append("%s=" % (tableName and '%s.%s' %
                                    (tableName, generic) or generic) + "%s")
                    paramStrings.append(self.normalizeTerm(s))
            return '(' + ' or '.join(clauseStrings) + ')', paramStrings
        elif isinstance(value, (QuotedGraph, Graph)):
            return "%s=" % (tableName and '%s.%s' %
                                (tableName, generic) or generic) + \
                                "%s", [self.normalizeTerm(value.identifier)]
        elif value == 'NULL':
            return "%s is null" % (tableName and '%s.%s' %
                                (tableName, generic) or generic), []
        else:
            return value is not None and "%s=" % (tableName and '%s.%s' %
                (tableName, generic) or generic) + "%s", [value] or None

    def _normalizeSQLCmd(self, cmd):
        """
        Normalize a SQL command before executing it.
        """
        if PY3:
            if not isinstance(cmd, str):
                cmd = str(cmd, 'ascii')
            return cmd
        else:
            #   Commence unicode black magic
            import types
            if not isinstance(cmd, types.UnicodeType):
                cmd = unicode(cmd, 'ascii')
            return cmd.encode('utf-8')

    def buildSubjClause(self, subject, tableName):
        return self.buildGenericClause("subject", subject, tableName)

    def buildPredClause(self, predicate, tableName):
        return self.buildGenericClause("predicate", predicate, tableName)

    def buildObjClause(self, obj, tableName):
        return self.buildGenericClause("object", obj, tableName)

    def buildContextClause(self, context, tableName):
        context = context is not None \
                            and self.normalizeTerm(context.identifier) \
                            or context
        return self.buildGenericClause("context", context, tableName)

    def buildTypeMemberClause(self, subject, tableName):
        return self.buildGenericClause("member", subject, tableName)

    def buildTypeClassClause(self, obj, tableName):
        return self.buildGenericClause("klass", obj, tableName)


CREATE_ASSERTED_STATEMENTS_TABLE = """\
CREATE TABLE %s_asserted_statements (
    subject       text not NULL,
    predicate     text not NULL,
    object        text not NULL,
    context       text not NULL,
    termComb      smallint not NULL)"""

CREATE_ASSERTED_TYPE_STATEMENTS_TABLE = """\
CREATE TABLE %s_type_statements (
    member        text not NULL,
    klass         text not NULL,
    context       text not NULL,
    termComb      smallint not NULL)"""

CREATE_LITERAL_STATEMENTS_TABLE = """\
CREATE TABLE %s_literal_statements (
    subject       text not NULL,
    predicate     text not NULL,
    object        text,
    context       text not NULL,
    termComb      smallint not NULL,
    objLanguage   varchar(3),
    objDatatype   text)"""

CREATE_QUOTED_STATEMENTS_TABLE = """\
CREATE TABLE %s_quoted_statements (
    subject       text not NULL,
    predicate     text not NULL,
    object        text,
    context       text not NULL,
    termComb      smallint not NULL,
    objLanguage   varchar(3),
    objDatatype   text)"""

CREATE_NS_BINDS_TABLE = """\
CREATE TABLE %s_namespace_binds (
    prefix        varchar(20) UNIQUE not NULL,
    uri           text,
    PRIMARY KEY (prefix))"""


CREATE_TABLE_STMTS = [
    CREATE_ASSERTED_STATEMENTS_TABLE,
    CREATE_ASSERTED_TYPE_STATEMENTS_TABLE,
    CREATE_QUOTED_STATEMENTS_TABLE,
    CREATE_NS_BINDS_TABLE,
    CREATE_LITERAL_STATEMENTS_TABLE
]
INDICES = [
    (
        "%s_asserted_statements",
        [
            ("%s_A_termComb_index", ('termComb', )),
            ("%s_A_s_index", ('subject', )),
            ("%s_A_p_index", ('predicate', )),
            ("%s_A_o_index", ('object', )),
            ("%s_A_c_index", ('context', )),
            ],
        ),
    (
        "%s_type_statements",
        [
            ("%s_T_termComb_index", ('termComb', )),
            ("%s_member_index", ('member', )),
            ("%s_klass_index", ('klass', )),
            ("%s_c_index", ('context', )),
            ],
        ),
    (
        "%s_literal_statements",
        [
            ("%s_L_termComb_index", ('termComb', )),
            ("%s_L_s_index", ('subject', )),
            ("%s_L_p_index", ('predicate', )),
            ("%s_L_c_index", ('context', )),
            ],
        ),
    (
        "%s_quoted_statements",
        [
            ("%s_Q_termComb_index", ('termComb', )),
            ("%s_Q_s_index", ('subject', )),
            ("%s_Q_p_index", ('predicate', )),
            ("%s_Q_o_index", ('object', )),
            ("%s_Q_c_index", ('context', )),
            ],
        ),
    (
        "%s_namespace_binds",
        [
            ("%s_uri_index", ('uri', )),
            ],
        )]
