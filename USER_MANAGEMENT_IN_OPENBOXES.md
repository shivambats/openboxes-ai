# User management in OpenBoxes

## Delayed-shipment sidecar account

The newer `sidecar_bot` workflow uses `ROLE_SIDECAR_BOT`, a stricter role than
`ROLE_SHIPMENT_CLERK`: shipment reads, ETA-only edits, comments, and allowlisted
custom events. Its API checks the account's warehouse rather than trusting the
session's selected warehouse. It cannot access the generic API or general
stock-movement mutation endpoints. The four requested capability names
(`VIEW_SHIPMENT`, `EDIT_SHIPMENT`, `ADD_COMMENT`, `CREATE_EVENT`) are implemented
by this role and endpoint policy, not native OpenBoxes permission records.

Deploy the sidecar migration and run the superuser-only sandbox seed to create
the account and two test shipments. Existing credentials and shipment histories
are preserved on repeated setup. See [the sidecar setup guide](sidecar/README.md)
for commands, environment variables, API contracts and verification.

This guide records how users, roles, location access, passwords, and the
shipment-scoped service account work in this OpenBoxes codebase.

## Data model

OpenBoxes models a user across several tables and domain objects:

- `Person` owns identity fields such as first name, last name, email, and the
  `active` flag (`person` table).
- `User extends Person` owns username, password hash, preferred warehouse,
  locale/timezone, and global roles (`user` and `user_role` tables).
- `Role` stores a `RoleType` enum value. Global roles apply everywhere.
- `LocationRole` assigns one role to a user at one location. Effective roles
  are the union of global roles and roles for the active location.

Relevant source:

- `grails-app/domain/org/pih/warehouse/core/User.groovy`
- `grails-app/domain/org/pih/warehouse/core/Person.groovy`
- `grails-app/domain/org/pih/warehouse/core/Role.groovy`
- `grails-app/domain/org/pih/warehouse/core/LocationRole.groovy`
- `src/main/groovy/org/pih/warehouse/core/RoleType.groovy`

## How authorization works

The main authorization check is `RoleInterceptor`. Most permissions use a
hierarchical primary role:

`Superuser > Admin > Manager/Assistant > Browser > Authenticated`

Supplemental roles add a specific capability such as invoicing or product
management. A role name by itself does not grant access: the interceptor or a
service must explicitly check it.

This matters for `ROLE_SHIPMENT_CLERK`. The enum existed before this work, but
the role was not seeded, shown by normal role management, accepted by the
location chooser, or checked by authorization. Assigning it alone therefore
did not create a working shipment user. Assigning Assistant or Manager would
enable shipment writes, but would also grant unrelated application writes and
would violate least privilege.

The implementation now treats `ROLE_SHIPMENT_CLERK` as a strict,
default-deny role. `RoleInterceptor.shipmentClerkActions` is the complete
allowlist. It permits non-destructive shipment and stock-movement reads/writes
and the read-only lookup endpoints required by those workflows. It denies:

- shipment, receipt, item, document, and comment deletion;
- shipment rollback;
- receiving and inventory adjustment;
- purchasing, orders, invoices, products writes, and user administration;
- every controller/action not explicitly listed.

The role is deliberately checked before the normal broad role hierarchy. Do
not combine it with Assistant, Manager, Admin, or Superuser. If it is combined
accidentally, the shipment allowlist remains the effective ceiling.

Relevant source and tests:

- `grails-app/controllers/org/pih/warehouse/RoleInterceptor.groovy`
- `grails-app/services/org/pih/warehouse/core/UserService.groovy`
- `src/test/groovy/org/pih/warehouse/RoleInterceptorSpec.groovy`
- `grails-app/migrations/0.9.x/changelog-2026-09-22-1200-add-shipment-clerk-role.xml`

## Normal user-management workflow

Use the OpenBoxes UI or `UserService`, not direct SQL, for routine work:

1. An Admin-or-higher user opens the user-management screen (`user.list` is
   admin-restricted even though some individual user mutations use the broader
   manager check).
2. Create the `User`/`Person` record and pass requested role IDs separately to
   `UserService.saveUser`.
3. For changes, use `UserService.updateUser`. Controllers must strip all role
   keys from bound parameters and send them as the dedicated role argument.
4. Assign either global roles or location roles. Prefer location roles when a
   human account should only work at specific facilities.
5. Activate the account and set its initial password.
6. Verify the effective roles at the target location and test both allowed and
   forbidden actions.

`UserService` validates whether the requesting user is allowed to add or remove
each role. This prevents mass-assignment of privileged roles. Passwords are
encoded by `PasswordCodec` as Base64-encoded SHA-1 for compatibility with this
legacy application. New integrations should not reproduce this design outside
OpenBoxes; use a modern password hashing algorithm when the application can be
migrated.

## Shipment delayed agent created locally

The local database contains:

| Field | Value |
| --- | --- |
| Username | `shipment-delayed-agent` |
| Display name | Shipment Delayed Agent |
| Active | Yes |
| Preferred warehouse | Main Warehouse (`1`) |
| Timezone | `Asia/Kolkata` |
| Global role | `ROLE_SHIPMENT_CLERK` only |

The temporary password is intentionally not stored in this repository. Rotate
it after the first successful login and keep the replacement in the local
secret manager used by the agent runtime.

This account was bootstrapped directly in the local database because the
running released image did not yet contain the new role. That is an exceptional
bootstrap step, not the recommended recurring process. The Liquibase migration
makes role creation repeatable for future deployments; create subsequent users
through the application service/UI after deploying the updated code.

## Verification checklist

After deploying the updated application:

1. Log in as `shipment-delayed-agent` and confirm Main Warehouse is selected.
2. Confirm shipment list/detail reads and allowed shipment updates succeed.
3. Confirm product lookup works only as read-only supporting data.
4. Confirm inventory browsing/adjustment, orders, purchasing, invoices, and
   user management return HTTP 403.
5. Confirm shipment deletion, rollback, and receiving return HTTP 403.
6. Query `user_role` and verify `ROLE_SHIPMENT_CLERK` is the account's only
   global role.
7. Re-run `RoleInterceptorSpec` whenever the allowlist changes.

When adding a new shipment endpoint, do not use a controller wildcard. Add the
single required action to `shipmentClerkActions`, add an allow/deny test, and
verify that the endpoint cannot mutate inventory or an unrelated business
object as a side effect.
