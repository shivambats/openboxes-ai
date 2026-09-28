<html>
<head>
    <meta name="layout" content="custom" />
    <title>Shipment human reviews</title>
    <content tag="pageTitle">Shipment human reviews</content>
</head>
<body>
<div class="body">
    <h1>Shipment human reviews</h1>
    <p>Review reported shipment conditions, verify the current state, and record your decision.</p>
    <g:if test="${flash.message}"><div class="message" role="status">${flash.message.encodeAsHTML()}</div></g:if>
    <g:if test="${flash.error}"><div class="errors" role="alert">${flash.error.encodeAsHTML()}</div></g:if>
    <g:if test="${!reviews}">
        <div class="message" role="status">No shipments currently need human review.</div>
    </g:if>
    <g:else>
        <table class="table table-striped">
            <thead><tr><th>Shipment</th><th>Reported condition</th><th>Action for reviewer</th><th>Decision</th></tr></thead>
            <tbody>
            <g:each in="${reviews}" var="review">
                <tr>
                    <td>
                        <g:link controller="shipment" action="showDetails" id="${review.shipment_id}">${review.shipment_number.encodeAsHTML()}</g:link><br/>
                        ${review.origin?.encodeAsHTML()} → ${review.destination?.encodeAsHTML()}<br/>
                        <small>${review.created_at?.encodeAsHTML()}</small>
                    </td>
                    <td>
                        <strong>${review.review_reason?.replace('_', ' ')?.encodeAsHTML()}</strong><br/>
                        <span>${review.evidence_quote?.encodeAsHTML()}</span>
                    </td>
                    <td>
                        <g:if test="${review.requested_action == 'CONFIRM_PAYMENT_AND_SHIPPING_STATE'}">
                            Confirm payment clearance and whether the shipment has left its origin. Review the delivery ETA.
                        </g:if>
                        <g:elseif test="${review.requested_action == 'CONFIRM_HOLD_AND_SHIPPING_STATE'}">
                            Confirm whether the reported condition still prevents dispatch, whether the shipment has left its origin, and the delivery ETA.
                        </g:elseif>
                        <g:elseif test="${review.requested_action == 'INSPECT_DAMAGE_AND_RECORD_DISPOSITION'}">
                            Inspect the reported damage, record the affected quantity and disposition, and update the receipt or inventory as appropriate.
                        </g:elseif>
                        <g:else>Inspect the shipment and its event history, then record the operational decision.</g:else>
                    </td>
                    <td>
                        <g:form controller="shipmentReview" action="resolve" method="POST">
                            <g:hiddenField name="shipmentId" value="${review.shipment_id}" />
                            <g:hiddenField name="reviewEventId" value="${review.review_event_id}" />
                            <label for="decision-${review.review_event_id}">Decision and action taken</label>
                            <textarea id="decision-${review.review_event_id}" name="decision" rows="3" cols="32" required minlength="10" maxlength="2000"></textarea><br/>
                            <button type="submit" class="button">Mark reviewed</button>
                        </g:form>
                    </td>
                </tr>
            </g:each>
            </tbody>
        </table>
    </g:else>
</div>
</body>
</html>
