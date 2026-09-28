package org.pih.warehouse.shipping

import org.pih.warehouse.core.User
import org.pih.warehouse.core.Location

/** Human work queue for shipment updates that require an operational decision. */
class ShipmentReviewController {
    ShipmentSidecarService shipmentSidecarService

    static allowedMethods = [index: 'GET', resolve: 'POST']

    def index() {
        User user = User.get(session.user?.id)
        try {
            [reviews: shipmentSidecarService.humanReviewQueue(user,
                    Location.get(session.warehouse?.id))]
        } catch (SecurityException ignored) {
            render status: 403, text: 'Shipment review access denied'
        }
    }

    def resolve() {
        User user = User.get(session.user?.id)
        try {
            shipmentSidecarService.resolveHumanReview(user,
                    Location.get(session.warehouse?.id), params.shipmentId,
                    params.reviewEventId, params.decision)
            flash.message = 'Review marked resolved. The decision was recorded on the shipment.'
        } catch (SecurityException ignored) {
            render status: 403, text: 'Shipment review access denied'
            return
        } catch (IllegalArgumentException ignored) {
            flash.error = 'Enter a decision of at least 10 characters.'
        } catch (IllegalStateException ignored) {
            flash.error = 'This review has already been resolved or changed. Reload the queue.'
        }
        redirect(action: 'index')
    }
}
