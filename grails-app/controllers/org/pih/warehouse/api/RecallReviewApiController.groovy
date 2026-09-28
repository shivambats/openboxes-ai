package org.pih.warehouse.api

import grails.converters.JSON
import org.pih.warehouse.core.User
import org.pih.warehouse.inventory.RecallReviewService

/** Review-only API. Holding or releasing stock is deliberately a separate operation. */
class RecallReviewApiController {
    RecallReviewService recallReviewService
    static allowedMethods = [markAttention: 'POST', resolve: 'POST']

    private void respondWith(Closure action) {
        try {
            render([data: action.call()] as JSON)
        } catch (SecurityException ignored) {
            render status: 403, contentType: 'application/json', text: '{"error":"Recall review access denied"}'
        } catch (IllegalArgumentException ignored) {
            render status: 400, contentType: 'application/json', text: '{"error":"Invalid recall review request"}'
        } catch (java.net.URISyntaxException ignored) {
            render status: 400, contentType: 'application/json', text: '{"error":"Invalid source URL"}'
        }
    }

    def markAttention() {
        respondWith { recallReviewService.markAttention(User.get(session.user?.id), request.JSON as Map) }
    }

    def resolve() {
        respondWith { recallReviewService.resolve(User.get(session.user?.id), request.JSON as Map) }
    }
}
