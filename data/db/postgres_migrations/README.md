# PostgreSQL migrations

`schema_postgres.sql` is the canonical schema used for a new database. Once a
release can create persistent production data, any in-place table change also
needs an immutable migration in this directory.

Files are named `NNNN_short_description.sql`, beginning with `0002`. Versions
must be consecutive. The startup path applies each file exactly once, records
its SHA-256 checksum in `marketmind_meta.schema_migrations`, and refuses to
start if an applied file changes or the database is newer than the checkout.
Fresh databases receive the final canonical schema and stamp all migrations as
already represented by that snapshot. A database created before the migration
ledger existed is detected by its application tables: it adopts baseline
`0001`, executes every later migration normally, and only then reapplies the
canonical schema for additive objects.

When adding a migration:

1. Update `data/db/schema_postgres.sql` so fresh databases get the final shape.
2. Add the equivalent transition here for existing databases.
3. Add a migration test covering both the old and final shapes.

Never edit a migration after it has shipped; add the next version instead.
