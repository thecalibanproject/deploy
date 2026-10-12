# Single sign-on (SAML)

Acme Boards is a fictional product used in Caliban's demos.

## Set up SAML single sign-on

Single sign-on is available on the Business and Enterprise plans. A workspace admin opens
**Settings > Security > Single sign-on**, copies the ACS URL and the entity ID into the identity
provider, then uploads the identity provider's metadata XML. Test the connection with one account
before you turn on **Require SSO** for everyone.

## Recovery

Admins keep a password login as a recovery path while SSO is required, so a broken identity
provider never locks the workspace.
