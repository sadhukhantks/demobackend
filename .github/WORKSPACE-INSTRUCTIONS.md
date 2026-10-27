# Workspace Instructions for AI Agents

## Repository Overview

This workspace contains **four repositories**, but only **two are active development targets**:

### Active Development Repositories (READ/WRITE)
| Repository | Path | Description |
|------------|------|-------------|
| `vowerp3be` | `./` (this repo) | **Current backend** - FastAPI + SQLAlchemy, Python |
| `vowerp3ui` | `../vowerp3ui` (sibling) | **Current frontend** - Next.js 15 + React 19 + TypeScript |

### Reference-Only Repositories (READ-ONLY — DO NOT MODIFY)
| Repository | Path | Description |
|------------|------|-------------|
| `vow_backend_2.0` | `../awscc-github/vow_backend_2.0` (if cloned) | **Legacy backend** - Java Spring Boot microservices |
| `vow-ui-2.0` | `../awscc-github/vow-ui-2.0` (if cloned) | **Legacy frontend** - React (class components) + Redux Saga |

---

## ⚠️ Critical Guidelines

### DO NOT modify `vow_backend_2.0` or `vow-ui-2.0`
These legacy repositories are **archived references only**. They must never be:
- Edited or updated
- Used as templates for new code
- Treated as the source of truth for patterns

### What HAS changed between legacy and current:
| Aspect | Legacy (2.0) | Current (p3) |
|--------|--------------|--------------|
| **Backend Language** | Java (Spring Boot) | Python (FastAPI) |
| **Backend Architecture** | Microservices with separate JARs | Monolithic FastAPI app with routers |
| **Frontend Framework** | React 17 with class components | Next.js 15 with React 19 + App Router |
| **State Management** | Redux + Redux Saga | React hooks + Context + local state |
| **Styling** | SCSS files | Tailwind CSS + MUI theme tokens |
| **API Structure** | REST via Spring controllers | REST via FastAPI routers |
| **Database Access** | JPA/Hibernate entities | SQLAlchemy ORM + raw `text()` queries |
| **Database Schema** | Similar tables but **naming conventions differ** | Updated column names, new tables |
| **Authentication** | Spring Security + JWT | FastAPI dependencies + JWT with refresh tokens |
| **Build/Deploy** | Maven multi-module | Docker + pip/pnpm |

---

## When to Reference Legacy Repositories

Use `vow_backend_2.0` and `vow-ui-2.0` **only** for:

1. **Understanding business logic** — What validations exist? What calculations are performed?
2. **Page flow reference** — What fields does a form have? What's the user journey?
3. **API contract discovery** — What data does an endpoint expect/return? (But adapt to new structure)
4. **Quick lookup** — Finding obscure validation rules or edge cases

### Examples of valid reference use:
```
✅ "Check vow-ui-2.0 to see what fields the Indent creation form has"
✅ "Look at vow_backend_2.0 to understand the GST calculation logic"
✅ "Reference legacy PO flow to ensure we don't miss any validation"
```

### Examples of INVALID use:
```
❌ Copying Java code patterns into Python
❌ Using Redux Saga patterns in the new React hooks architecture
❌ Assuming database column names are the same
❌ Modifying any file in the legacy repos
```

---

## Current Architecture Patterns (vowerp3be + vowerp3ui)

### Backend (vowerp3be)
- **Entry point**: `src/main.py`
- **Routers**: Feature-based in `src/{module}/` (e.g., `src/procurement/po.py`)
- **Queries**: Raw SQL via `sqlalchemy.text()` in `query.py` files
- **Multi-tenant**: `src/config/db.py` derives tenant DB from request headers
- **Auth**: JWT with refresh tokens in `src/authorization/`

### Frontend (vowerp3ui)
- **Pages**: `src/app/dashboard{portal|admin|ctrldesk}/...`
- **UI Components**: `src/components/ui/` — wrappers around MUI
- **Page Archetypes**: Index (listing), Transaction (form+grid), Report, Dashboard
- **Hooks**: `src/hooks/` for shared logic
- **API Client**: `src/utils/apiClient2.ts` with `fetchWithCookie`

Refer to:
- `vowerp3be/.github/copilot-instructions.md` for backend conventions
- `vowerp3ui/.github/copilot-instructions.md` for frontend conventions

---

## Summary

| Action | vowerp3be | vowerp3ui | vow_backend_2.0 | vow-ui-2.0 |
|--------|-----------|-----------|-----------------|------------|
| Read files | ✅ | ✅ | ✅ | ✅ |
| Edit files | ✅ | ✅ | ❌ | ❌ |
| Use as code template | ✅ | ✅ | ❌ | ❌ |
| Reference for logic | ✅ | ✅ | ✅ | ✅ |
| Copy patterns from | ✅ | ✅ | ❌ | ❌ |

**When in doubt, always implement using patterns from `vowerp3be` and `vowerp3ui`.**
