from tests.access.test_access import OPERATOR, grant
from tests.core.test_product_controls import prepare
from tests.core.test_service import OWNER


async def test_company_access_change_never_revives_prior_approval(setup):
    s=setup.service
    grant(s,OPERATOR,'operator')
    post=await prepare(setup)
    s.approve_post(OPERATOR,post.id,post.revision)
    s.set_company_access(OWNER,post.business_id,OPERATOR,'none','selected')
    s.set_company_access(OWNER,post.business_id,OPERATOR,'operator','selected')
    assert not s.repo.approval_allowed(OWNER,s.repo.get_post(post.id))
    setup.clock.advance(minutes=2)
    assert not await s.publish_due_once()
    assert not setup.publisher.calls
