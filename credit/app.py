"""应用装配：一个 BankApp 持有存储与全部领域服务。"""

from credit.collections import CollectionService
from credit.compliance import ComplianceService
from credit.consent import ConsentService
from credit.creditline import CreditLineService
from credit.customers import CustomerService
from credit.disclosure import DisclosureService
from credit.hardship import HardshipService
from credit.marketing import MarketingService
from credit.monitoring import MonitoringService
from credit.review import ReviewService
from credit.store import Store
from credit.underwriting import UnderwritingService


class BankApp:
    def __init__(self, db_path: str = ":memory:"):
        self.store = Store(db_path)
        self.consents = ConsentService(self.store)
        self.customers = CustomerService(self.store)
        self.underwriting = UnderwritingService(self.store, self.consents)
        self.credit_lines = CreditLineService(self.store, self.consents)
        self.monitoring = MonitoringService(self.store, self.consents)
        self.reviews = ReviewService(self.store)
        self.hardship = HardshipService(self.store)
        self.collections = CollectionService(self.store, self.hardship)
        self.compliance = ComplianceService(self.store, self.consents)
        self.disclosure = DisclosureService(self.store)
        self.marketing = MarketingService(self.store, self.consents)
