# ClashArena
ClashArena (CM0639-1) repository - represents our project for the course SOFTWARE ARCHITECTURES (CM90) - a.a. 2025-26

An online esports tournament platform for managing competitive video-game tournaments, including tournament creation, registration, seeding, brackets, match scheduling, result validation, ratings, leaderboards, and live information.

🔧 Development
Student	Core Domain / Feature Responsibility	Architectural Responsibility
Daniil Glazunov	Identity & Access: Account registration, authentication, player profiles, teams, roles and access control.	Security & Integrity: Authentication, authorization, RBAC, protection of match results and ratings, auditability, and data protection.
Shattyk Kuziyeva	Tournament Management: Tournament formats, registration/check-in, seeding, groups, brackets and match scheduling.	Scalability & Elasticity: Designing the system to support long-term growth and sudden event-day traffic spikes.

Phase 1: Architecture & Core Foundations
Assignee	Task / Issue	Definition of Done
Daniil	[Security] Identity & Access Design	Define users, roles and access rules. Design the authentication and authorization flow.
Shattyk	[Tournament] Domain Design	Define tournament formats, registration, check-in, seeding, groups, brackets and match scheduling.

Phase 2: Core Feature Implementation
Assignee	Task / Issue	Definition of Done
Daniil	[Backend] Authentication & Authorization	Users can register and authenticate, and access is controlled according to their roles.
Daniil	[Security] Integrity & Auditability	Match results and rating changes are protected and important operations can be traced and audited.
Shattyk	[Backend] Tournament Management	Organisers can create tournaments, select formats, manage registration and check-in, and manage participants.
Shattyk	[Backend] Seeding, Brackets & Scheduling	Participants can be seeded, groups/brackets generated, and matches assigned to scheduled time slots.

Phase 3: Architectural Characteristics
Assignee	Task / Issue	Definition of Done
Daniil	[Security] Data Protection	Sensitive data is protected in transit and at rest, with appropriate authorization controls.
Daniil	[Integrity] Result Validation	Match results and rating changes are validated, protected from unauthorized modification, and auditable.
Shattyk	[Scalability] Growth Support	The architecture can support 10× growth in registered accounts and 5× growth in concurrent competitors without redesign.
Shattyk	[Elasticity] Event Traffic	The architecture can handle sudden spectator traffic increases through automatic capacity scaling and release of capacity after the event.

Phase 4: Integration & Testing
Assignee	Task / Issue	Definition of Done
Daniil	[QA] Security & Integrity Testing	Test authentication, authorization and integrity of results/rating changes; fix identified issues.
Daniil	[Docs] Security Architecture	Document authentication, authorization, integrity and audit mechanisms.
Shattyk	[QA] Scalability & Load Testing	Test the system under increasing competitor/spectator loads and document the results.
Shattyk	[Docs] Scalability & Elasticity Architecture	Document scaling strategy, autoscaling and how the system handles event-day traffic spikes.
