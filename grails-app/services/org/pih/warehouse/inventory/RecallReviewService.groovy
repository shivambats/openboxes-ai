package org.pih.warehouse.inventory

import grails.gorm.transactions.Transactional
import java.net.URI
import org.pih.warehouse.core.RoleType
import org.pih.warehouse.core.User
import org.pih.warehouse.product.Product

/** A review marker on a product lot. It does not change lotStatus or pickability. */
@Transactional
class RecallReviewService {
    static final Set<String> REASONS = [
            'SEARCH_NO_MATCH', 'NOTICE_DOES_NOT_MATCH', 'VERIFIED_MATCH_PENDING_APPROVAL'
    ] as Set

    private static void authorize(User user) {
        if (!user?.active || !user.roles.any { it.roleType == RoleType.ROLE_SUPERUSER }) {
            throw new SecurityException('Superuser required for recall review marker')
        }
    }

    private static InventoryItem lotFor(Map body) {
        String code = body.productCode instanceof String ? body.productCode.trim() : ''
        String ndc = body.ndc instanceof String ? body.ndc.trim() : ''
        String lotNumber = body.lotNumber instanceof String ? body.lotNumber.trim() : ''
        if (!code || !ndc || !lotNumber) { throw new IllegalArgumentException('Product, NDC, and lot are required') }
        Product product = Product.findByProductCode(code)
        if (!product || product.ndc != ndc) { throw new IllegalArgumentException('Product and NDC do not match') }
        InventoryItem item = InventoryItem.findByProductAndLotNumber(product, lotNumber)
        if (!item) { throw new IllegalArgumentException('Lot does not exist') }
        item
    }

    Map markAttention(User user, Map body) {
        authorize(user)
        InventoryItem item = lotFor(body)
        String reason = body.reason instanceof String ? body.reason : ''
        String responseId = body.modelResponseId instanceof String ? body.modelResponseId.trim() : ''
        String sourceUrl = body.sourceUrl instanceof String ? body.sourceUrl.trim() : ''
        if (!(reason in REASONS) || !responseId || responseId.size() > 120 || sourceUrl.size() > 1000) {
            throw new IllegalArgumentException('Invalid recall review evidence')
        }
        if (sourceUrl) {
            URI uri = new URI(sourceUrl)
            String host = uri.host?.toLowerCase()
            if (uri.scheme != 'https' || !(host == 'fda.gov' || host?.endsWith('.fda.gov'))) {
                throw new IllegalArgumentException('Recall review source must be an FDA HTTPS URL')
            }
        }
        if (reason != 'SEARCH_NO_MATCH' && !sourceUrl) {
            throw new IllegalArgumentException('A verified notice URL is required')
        }
        if (item.recallReviewState != 'NEEDS_ATTENTION' || item.recallReviewReason != reason ||
                item.recallReviewSourceUrl != (sourceUrl ?: null) || item.recallReviewResponseId != responseId) {
            item.recallReviewState = 'NEEDS_ATTENTION'
            item.recallReviewReason = reason
            item.recallReviewSourceUrl = sourceUrl ?: null
            item.recallReviewResponseId = responseId
            item.recallReviewUpdatedAt = new Date()
            item.save(failOnError: true, flush: true)
        }
        snapshot(item)
    }

    Map resolve(User user, Map body) {
        authorize(user)
        InventoryItem item = lotFor(body)
        item.recallReviewState = null
        item.recallReviewReason = null
        item.recallReviewSourceUrl = null
        item.recallReviewResponseId = null
        item.recallReviewUpdatedAt = new Date()
        item.save(failOnError: true, flush: true)
        snapshot(item)
    }

    static Map snapshot(InventoryItem item) {
        [inventoryItemId: item.id, productCode: item.product.productCode, lotNumber: item.lotNumber,
         recallReviewState: item.recallReviewState, recallReviewReason: item.recallReviewReason,
         recallReviewSourceUrl: item.recallReviewSourceUrl, lotStatus: item.lotStatus?.name()]
    }
}
