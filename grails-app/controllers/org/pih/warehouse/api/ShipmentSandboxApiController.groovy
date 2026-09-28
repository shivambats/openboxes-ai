package org.pih.warehouse.api

import grails.converters.JSON
import org.pih.warehouse.core.User
import org.pih.warehouse.core.RoleType
import org.pih.warehouse.shipping.ShipmentSandboxService

class ShipmentSandboxApiController {
    ShipmentSandboxService shipmentSandboxService
    static allowedMethods = [seed: 'POST']

    def seed() {
        User user = User.get(session.user?.id)
        if (System.getenv('SIDECAR_SANDBOX_ENABLED') != 'true' ||
                !user?.active || !user.roles.any { it.roleType == RoleType.ROLE_SUPERUSER }) {
            render status: 403
            return
        }
        String password = request.JSON.password
        if (!password || password.size() < 16) {
            render status: 400, text: 'Supply a sidecar_bot password of at least 16 characters'
            return
        }
        render([data: shipmentSandboxService.seed(user, password)] as JSON)
    }
}
