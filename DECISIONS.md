# Design decisions

The unique employee-code index makes duplicate creates return one 201 and the rest 409. The unique `(emp_code, date)` index protects punch-in against races. The date/code index supports attendance ordering; the date/late index supports late ranking. The department/code index supports filtered employee lists, while department/join-date and join-date indexes support headcount lookups. I rejected indexes for every status and derived value because they add write/storage cost without helping the listed access patterns.

For two simultaneous punches, both requests read the employee and calculate the same attendance key. Both try to insert. MongoDB's unique index accepts one insert; the other raises `DuplicateKeyError`, which the API maps to 409. Correctness does not depend on a prior existence check.

The leaderboard uses MongoDB `$rank`, so ties share a rank and the next rank is skipped. The rank cutoff keeps every employee tied at that cutoff; code orders tied rows consistently. Department summary begins with employees joined by month end and looks up their logs, preserving employees whose lookup result is empty. For 100x the data I would benchmark the real workload, review explain plans, introduce cursor-based pagination, and consider partitioning or sharding only if measurements justified it.
