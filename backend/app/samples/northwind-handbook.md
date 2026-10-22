# Northwind Logistics: Employee Handbook (v4.2, 2026)

Northwind Logistics runs same-day and next-day freight for retailers across India. This handbook covers how we work. When a policy here conflicts with your offer letter, the offer letter wins.

## Working hours and remote work

Core collaboration hours are 11:00 to 16:00 IST, Monday to Friday. Outside core hours you can arrange your day as you like. Engineering, finance and people teams may work remotely up to three days a week. Warehouse operations and fleet dispatch staff work on site because the job needs it.

Anyone may work from another city for up to 20 working days a year. Working from outside India needs written approval from the People team at least two weeks in advance, because of tax and data-protection rules.

## Leave

Full-time employees get 24 days of paid annual leave per calendar year, accrued monthly at 2 days per month. Up to 8 unused days carry over into the next year; anything above that lapses on 31 March.

Sick leave is separate: 12 days per year, no doctor's note needed for absences of up to 2 consecutive days. Bereavement leave is 5 days for immediate family. Parental leave is 26 weeks for the primary caregiver and 6 weeks for the secondary caregiver, fully paid, available after 3 months of service.

## Expenses and reimbursement

Submit expenses in the Ledgerly app within 30 days of the purchase, with a photo of the receipt. Claims older than 60 days are not reimbursed. Approved claims are paid with the next payroll run, which happens on the last working day of each month.

Meals while travelling are reimbursed up to 1,500 INR per day in metro cities and 1,000 INR per day elsewhere. Alcohol is never reimbursed. Client entertainment above 10,000 INR in a single bill needs prior approval from your department head.

## Travel

Book flights and hotels through the Ledgerly travel desk. Economy class is the default for every flight. Business class is allowed only for flights longer than 6 hours and must be approved by a vice president. Hotels are capped at 7,000 INR per night in Mumbai, Delhi and Bengaluru and 4,500 INR per night in other cities.

Train travel is encouraged for trips under 500 km. Personal car use is reimbursed at 12 INR per kilometre.

## Refunds and customer credits

Support engineers can issue refunds of up to 5,000 INR on their own. Refunds above 5,000 INR need approval from a support manager, and refunds above 50,000 INR also need sign-off from the finance controller. Every refund must reference the shipment ID (format NW-SHP-XXXXXXX) in the ticket.

Service credits for late deliveries are applied automatically: 10 percent of the shipment fee if a same-day delivery arrives late, 25 percent if it arrives more than 4 hours late.

## On-call

The engineering on-call rotation changes every Tuesday at 10:00 IST. The outgoing engineer writes a handover note in the #oncall channel listing open incidents, risky deploys and anything unusual. On-call engineers receive an allowance of 3,000 INR per week, plus 1,500 INR for every page outside 09:00 to 21:00.

Acknowledge a page within 5 minutes. If you cannot, the page escalates to the secondary on-call, and after 15 minutes to the engineering manager.

## Incident severity levels

SEV1: the booking platform or driver app is down for all customers, or there is a confirmed data breach. Declare in #incidents, page the incident commander, and post a status page update within 15 minutes.

SEV2: a major feature is broken for many customers (for example live tracking or invoicing), or one region cannot dispatch. Status page update within 30 minutes.

SEV3: a minor feature is degraded with a workaround available. Fix during business hours.

Every SEV1 and SEV2 incident gets a blameless postmortem within 5 working days. Postmortems are stored in the Incidents space of the wiki.

## Deployments

We deploy to production Monday to Thursday between 10:00 and 17:00 IST. Friday deploys require a written rollback plan approved by the engineering manager. There is a code freeze from 20 December to 2 January every year because of peak season. Database migrations must be backward compatible with the previous release so that we can always roll back the application.

## Security basics

Use the company password manager for every work account. Multi-factor authentication is mandatory for email, the VPN, GitHub and the cloud console; hardware security keys are issued to everyone with production access.

Laptops must have full-disk encryption enabled and lock after 5 minutes of inactivity. Report a lost or stolen device to security@northwind.example within 1 hour. Never paste customer data, credentials or source code into external AI tools that are not on the approved list.

## Data retention

Shipment records are kept for 8 years to meet tax law. Driver GPS traces are kept for 90 days and then aggregated. Customer support call recordings are deleted after 180 days. Application logs are retained for 30 days in hot storage and 1 year in cold storage.

Customers can request deletion of their personal data; the privacy team must complete such requests within 30 days.

## Onboarding

New joiners get their laptop on day one and complete security training within their first week. Each new joiner is paired with an onboarding buddy for the first 60 days. Engineers ship a small change to production in their first two weeks. The probation period is 3 months, with a check-in conversation at 6 weeks.

## Performance reviews and pay

Performance reviews happen twice a year, in April and October. Salary revisions take effect from 1 April. The annual bonus is paid in June and depends on company results and individual performance. Employees can nominate peers for the quarterly Northstar award, which comes with a 25,000 INR gift voucher.

## Learning budget

Every employee has a learning budget of 40,000 INR per year for courses, books and conferences. Certifications relevant to your role are also covered, including exam fees, up to two attempts. Unused learning budget does not carry over.
