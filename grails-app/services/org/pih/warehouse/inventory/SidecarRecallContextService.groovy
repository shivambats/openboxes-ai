package org.pih.warehouse.inventory

import grails.gorm.transactions.Transactional
import org.pih.warehouse.core.RoleType
import org.pih.warehouse.core.User
import org.pih.warehouse.product.Product
import org.pih.warehouse.product.ProductAvailability

/**
 * Read-only recall context for the sidecar's local console.
 *
 * It returns the same product and stocked-lot view the operator chat builds for a
 * signed-in user, but only for synthetic test products, so the service account
 * never reads real stock through this path.
 */
@Transactional(readOnly = true)
class SidecarRecallContextService {
    static final String SYNTHETIC_PRODUCT_PREFIX = 'RECALL-TEST-'

    static boolean syntheticProduct(Product product) {
        product?.productCode?.toUpperCase(Locale.ROOT)?.startsWith(SYNTHETIC_PRODUCT_PREFIX)
    }

    private static void authorize(User user) {
        if (!user?.active || !user.roles.any { it.roleType == RoleType.ROLE_SIDECAR_BOT }) {
            throw new SecurityException('Sidecar role required')
        }
    }

    Map recallContext(User user, String productReference, String lotReference) {
        authorize(user)
        String product = productReference?.trim()
        String lot = lotReference?.trim() ?: null
        if (!product || product.length() > 255 || lot?.length() > 255) {
            throw new IllegalArgumentException('Name one product and at most one lot')
        }
        List<Product> matches = Product.executeQuery(
                'from Product p where lower(p.productCode) = :query or ' +
                'lower(p.name) = :query or lower(p.ndc) = :query',
                [query: product.toLowerCase(Locale.ROOT)], [max: 3])
        if (matches.size() != 1) {
            return [outcome: matches ? 'ambiguous_product' : 'product_not_found']
        }
        Product match = matches[0]
        if (!syntheticProduct(match)) {
            return [outcome: 'synthetic_product_required']
        }
        List rows = ProductAvailability.findAllByProduct(match).findAll { ProductAvailability item ->
            item.quantityOnHand > 0 && (!lot || item.lotNumber?.equalsIgnoreCase(lot))
        }.collect { ProductAvailability item ->
            [lotNumber: item.lotNumber,
             inventoryItemId: item.inventoryItem?.id,
             locationId: item.location?.id,
             warehouse: item.location?.name,
             bin: item.binLocation?.name ?: item.binLocationName ?: 'Unassigned',
             quantityOnHand: item.quantityOnHand,
             quantityOnHold: item.quantityOnHold ?: 0,
             recallReviewState: item.inventoryItem?.recallReviewState,
             lotStatus: item.inventoryItem?.lotStatus?.name()]
        }
        [outcome: 'found', recall_context: [productId: match.id,
                productCode: match.productCode, productName: match.name, ndc: match.ndc,
                lotReference: lot, inventory: rows,
                fetchedAt: java.time.Instant.now().toString()]]
    }
}
