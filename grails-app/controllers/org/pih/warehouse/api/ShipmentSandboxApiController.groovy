package org.pih.warehouse.api

import grails.converters.JSON
import grails.plugin.cache.CacheEvict
import org.pih.warehouse.core.User
import org.pih.warehouse.core.RoleType
import org.pih.warehouse.shipping.ShipmentSandboxService
import org.pih.warehouse.shipping.WarehouseFixtureService

class ShipmentSandboxApiController {
    ShipmentSandboxService shipmentSandboxService
    WarehouseFixtureService warehouseFixtureService
    static allowedMethods = [seed: 'POST', seedOutreach: 'POST', seedDemoDelays: 'POST', fixture: 'POST']

    def seedOutreach() {
        User user = User.get(session.user?.id)
        if (System.getenv('SIDECAR_SANDBOX_ENABLED') != 'true' ||
                !user?.active || !user.roles.any { it.roleType == RoleType.ROLE_SUPERUSER }) {
            render status: 403
            return
        }
        render([data: shipmentSandboxService.seedOutreachContacts()] as JSON)
    }

    def seedDemoDelays() {
        User user = User.get(session.user?.id)
        if (System.getenv('SIDECAR_SANDBOX_ENABLED') != 'true' ||
                !user?.active || !user.roles.any { it.roleType == RoleType.ROLE_SUPERUSER }) {
            render status: 403
            return
        }
        render([data: shipmentSandboxService.seedDemoDelays(user)] as JSON)
    }

    def seed() {
        User user = User.get(session.user?.id)
        if (System.getenv('SIDECAR_SANDBOX_ENABLED') != 'true' ||
                !user?.active || !user.roles.any { it.roleType == RoleType.ROLE_SUPERUSER }) {
            render status: 403
            return
        }
        String password = request.JSON.password
        if (!password || password.size() < 16) {
            render status: 400, text: 'Supply a vegath-agent password of at least 16 characters'
            return
        }
        render([data: shipmentSandboxService.seed(user, password)] as JSON)
    }

    /** Load the Lakeshore Health warehouse fixture, then recompute stock once it has committed. */
    // Dashboard numbers are cached, so a fixture load clears them.
    @CacheEvict(value = 'dashboardCache', allEntries = true)
    def fixture() {
        User user = User.get(session.user?.id)
        if (System.getenv('SIDECAR_SANDBOX_ENABLED') != 'true' ||
                !user?.active || !user.roles.any { it.roleType == RoleType.ROLE_SUPERUSER }) {
            render status: 403
            return
        }
        Map result = warehouseFixtureService.load(user)
        result.stock = warehouseFixtureService.refreshStock()
        render([data: result] as JSON)
    }
}
