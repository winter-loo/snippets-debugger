# Usage

## display PostgreSQL memory context tree

```
(lldb) command script import ./pg_memcxt_stats.py
(lldb) pgmem CacheMemoryContext
```

NOTE: Your debugging session should be interrupted state to execute commands above.

For more usage of `pgmem`, execute `pgmem -h`.

## simple case

```
(lldb) command script import expression_address.py
(lldb) command script import breakpoint_set.py
```

## show stack trace of PostgreSQL memory API

```
$ psql
(psql) select pg_backend_pid();
12345
```

```
$ lldb -p 12345
(lldb) command script import trace_pg_mem.py
```

```
(psql) select 1;
```

```
(lldb) trace_custom_api
```

```
$ nvim trace_pg_mem.txt
```
