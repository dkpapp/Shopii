# Graphql.py
#
# Optimized GraphQL queries strictly tailored to bypass schema anomaly detection.
# Added metadata/analytics variables in the mutation to match standard browser payloads.

QUERY_PROPOSAL_SHIPPING = """
query Proposal(
  $delivery: DeliveryTermsInput,
  $discounts: DiscountTermsInput,
  $payment: PaymentTermInput,
  $merchandise: MerchandiseTermInput,
  $buyerIdentity: BuyerIdentityTermInput,
  $taxes: TaxTermInput,
  $sessionInput: SessionTokenInput!,
  $checkpointData: String,
  $queueToken: String
) {
  session(sessionInput: $sessionInput) {
    negotiate(
      input: {
        purchaseProposal: {
          delivery: $delivery,
          discounts: $discounts,
          payment: $payment,
          merchandise: $merchandise,
          buyerIdentity: $buyerIdentity,
          taxes: $taxes
        },
        checkpointData: $checkpointData,
        queueToken: $queueToken
      }
    ) {
      __typename
      result {
        ... on NegotiationResultAvailable {
          checkpointData
          queueToken
          sellerProposal {
            delivery {
              __typename
              ... on FilledDeliveryTerms {
                deliveryLines {
                  availableDeliveryStrategies {
                    ... on CompleteDeliveryStrategy { handle }
                  }
                }
              }
              ... on PendingTerms { pollDelay }
            }
            runningTotal {
              __typename
              ... on MoneyValueConstraint { value { amount currencyCode } }
            }
            payment {
              __typename
              ... on FilledPaymentTerms {
                availablePaymentLines {
                  paymentMethod {
                    __typename
                    ... on PaymentProvider { paymentMethodIdentifier name }
                    ... on OffsiteProvider { paymentMethodIdentifier name }
                    ... on CustomOnsiteProvider { paymentMethodIdentifier name }
                    ... on CustomerCreditCardPaymentMethod { paymentMethodIdentifier name }
                  }
                }
              }
              ... on PendingTerms { pollDelay }
            }
          }
        }
        ... on CheckpointDenied { redirectUrl }
        ... on Throttled { pollAfter }
      }
    }
  }
}
"""

QUERY_PROPOSAL_DELIVERY = QUERY_PROPOSAL_SHIPPING

MUTATION_SUBMIT = """
mutation SubmitForCompletion(
  $input: NegotiationInput!, 
  $attemptToken: String!,
  $analytics: CheckoutAnalyticsInput
) {
  submitForCompletion(
    input: $input, 
    attemptToken: $attemptToken,
    analytics: $analytics
  ) {
    __typename
    ... on SubmitSuccess { 
      receipt { 
        __typename 
        ... on ProcessedReceipt { id } 
        ... on ProcessingReceipt { id } 
        ... on WaitingReceipt { id } 
        ... on ActionRequiredReceipt { id } 
        ... on FailedReceipt { id } 
      } 
    }
    ... on SubmitAlreadyAccepted { 
      receipt { 
        __typename 
        ... on ProcessedReceipt { id } 
      } 
    }
    ... on SubmittedForCompletion { 
      receipt { 
        __typename 
        ... on ProcessedReceipt { id } 
      } 
    }
    ... on SubmitFailed { reason }
    ... on SubmitRejected {
      errors {
        __typename
        ... on NegotiationError { code localizedMessage }
      }
    }
    ... on CheckpointDenied { redirectUrl }
  }
}
"""

QUERY_POLL = """
query PollForReceipt($receiptId: ID!, $sessionToken: String!) {
  receipt(receiptId: $receiptId, sessionInput: {sessionToken: $sessionToken}) {
    __typename
    ... on ProcessedReceipt { id }
    ... on ActionRequiredReceipt { id }
    ... on FailedReceipt {
      id
      processingError {
        __typename
        ... on PaymentFailed { code }
      }
    }
    ... on ProcessingReceipt { id pollDelay }
    ... on WaitingReceipt { id pollDelay }
  }
}
"""