# Full core test-suite baseline

Updated: 2026-11-21

The workflow now executes `python manage.py test core --verbosity 2`. This is intentionally broader than the former selected test modules.

## Observed result

Run for commit `ed596fd0c2ad37f9c818e643929cc4ee536c076e`:

- 470 tests executed
- 32 failures
- 25 errors
- Django configuration, static collection, license CLI, migration-state check, and Linux shell syntax checks passed.

The next run on `d83194dc14bb0a9d5114721fa8a5585392c61592` is in progress at the time this note was written.

## Failure clusters

1. Legacy tests in `core/tests.py` create products/users without assigning a Company and active StaffMembership. New installation/setup and company-context middleware now blocks or redirects these requests, so tests that expect POS JSON or stock movement fail.
2. A number of legacy catalog, car-parts, currency, demo-data, and finance tests also assume the pre-single-installation context. Some request tests receive redirects, making `response.context` unavailable or JSON parsing fail.
3. Some tenant-aware tests have fixture collisions (e.g. duplicate company slug) and require fixture isolation/unique identifiers.
4. Supplier account ledger has at least one balance assertion that differs from current credit-note/compensation accounting. This must be reviewed against the intended ledger semantics, not blindly changed.

## Remediation policy

- Do not weaken production company/access/license middleware merely to make stale tests pass.
- Update test fixtures to create a local Company and explicit StaffMembership using `core.tests_tenant_helpers.create_test_company`, and attach company ownership to operational records.
- Fix duplicate fixture slugs and use unique company names/owner identifiers.
- Review ledger mismatches as business-rule defects; add regression coverage for the correct accounting result.
- Keep full-core test discovery enabled until the baseline is understood. Do not replace it with a smaller green-only list to conceal failures.
- A commercial release remains blocked until the full suite is green or any intentionally obsolete tests are removed with documented replacement coverage.

