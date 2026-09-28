<g:if test="${inventoryItem?.recallReviewState == 'NEEDS_ATTENTION' || reviewCount}">
    <g:set var="reviewExplanation" value="${reviewCount ? 'Open Manage lot numbers to review marked lots' : (inventoryItem.recallReviewReason == 'SEARCH_NO_MATCH' ? 'No matching FDA notice found in the bounded search; quality review required' : (inventoryItem.recallReviewReason == 'NOTICE_DOES_NOT_MATCH' ? 'FDA notice found, but its NDC and lot do not both match; quality review required' : 'Matching FDA notice found; hold approval required'))}"/>
    <span class="recall-review-attention" title="${reviewExplanation}"
          style="display:inline-block;padding:3px 8px;border:1px solid #f2dfb1;border-radius:4px;background:#fff8e8;color:#795500;font-size:12px;font-weight:600;white-space:nowrap;">
        Needs attention<g:if test="${reviewCount}"> (${reviewCount})</g:if>
    </span>
    <g:if test="${inventoryItem?.recallReviewSourceUrl}">
        <a href="${inventoryItem.recallReviewSourceUrl}" target="_blank" rel="noopener noreferrer"
           title="FDA recall notice">FDA notice</a>
    </g:if>
</g:if>
