# Human Taste: Code Examples

Worked examples showing the code taste rubric applied to realistic scenarios.

---

## Example 1: Express.js API Controller

**User prompt:** "Review this user controller -- something feels off about the design."

### Human Taste: Code Report

**Subject:** `userController.js` -- Express API controller handling CRUD + auth
**Language:** JavaScript (Node.js)
**Date:** 2026-02-25
**Overall Score:** 2.6 / 5

#### Scores

| Dimension | Score | Key Evidence |
|-----------|-------|-------------|
| Abstraction Depth | 2/5 | Controller functions directly call Prisma, format responses, validate input, and send emails -- no layering |
| Conceptual Integrity | 2/5 | `createUser` returns `{ success: true, data }`, `getUser` returns `{ user }`, `deleteUser` returns `{ message }` -- three response formats |
| Change Cost | 2/5 | Switching from Prisma to another ORM requires editing every controller function; email logic is inline |
| Simplicity | 3/5 | Logic is straightforward; no unnecessary abstraction -- but this is accidental simplicity from insufficient structure |
| Readability | 3/5 | Decent naming; functions are 40-60 lines each which is borderline |
| Robustness | 3/5 | Try/catch around database calls but error messages expose internal details (`error.message` sent to client) |

#### Strengths
- Function names clearly describe intent (`createUser`, `getUserById`, `updateUserEmail`)
- No premature abstraction -- the code is honest about what it does

#### Issues
- **Critical**: No response format consistency -- callers cannot predict the shape of responses -- introduce a `respond(res, data, status)` helper
- **Major**: Business logic (validation, email sending) lives in the controller -- extract a `UserService` so the controller only handles HTTP concerns
- **Major**: Raw `error.message` leaked to client in catch blocks -- use a sanitized error mapper
- **Minor**: Magic strings for roles (`"admin"`, `"user"`) scattered across functions -- extract to constants

#### Verdict
Honest code that does the job but will become painful to modify. The highest-impact refactor is extracting a service layer -- it would fix the response inconsistency, make email logic testable, and cut controller functions to 10 lines each.

---

## Example 2: Comparing Two Data Processing Approaches

**User prompt:** "I have two implementations for our CSV import pipeline. Which one has better design taste?"

### Comparison Table

| Dimension | Approach A (procedural) | Approach B (class hierarchy) |
|-----------|------------------------|------------------------------|
| Abstraction Depth | 3/5 | 2/5 |
| Conceptual Integrity | 4/5 | 2/5 |
| Change Cost | 3/5 | 2/5 |
| Simplicity | 4/5 | 1/5 |
| Readability | 4/5 | 2/5 |
| Robustness | 3/5 | 3/5 |
| **Overall** | **3.4** | **2.1** |

**Winner: Approach A** (3.4 vs 2.1)

**Analysis:** Approach B uses `AbstractCSVProcessor` -> `ValidatingProcessor` -> `TransformingProcessor` -> `PersistingProcessor` -- four levels of inheritance for what is fundamentally a linear pipeline. Each class overrides a single method. This is the "astronaut architecture" anti-pattern: inheritance depth that adds indirection without hiding complexity.

Approach A uses three functions (`validate_rows`, `transform_rows`, `persist_rows`) composed in a pipeline: `persist_rows(transform_rows(validate_rows(read_csv(path))))`. Each function is 15-25 lines. Adding a new step means writing one function and inserting it in the pipeline.

**Tradeoff:** Approach B handles errors slightly better because each processor class has its own error context. Borrowing that idea (wrapping each pipeline step with error context) into Approach A would give you the best of both.

---

## Example 3: AI-Generated React Component

**User prompt:** "Cursor generated this React component for me. Does the code pass the human taste test?"

### Key Findings

The AI-generated `DataTable` component (280 lines) shows three classic AI code taste failures:

1. **Shallow modules**: The component accepts 14 props including `onSort`, `onFilter`, `onPaginate`, `onRowSelect`, `onColumnResize`, `renderHeader`, `renderCell`, `renderFooter`, `renderEmpty`. The interface is nearly as complex as building the table yourself. A tasteful API would accept `columns`, `data`, and `options` -- hiding pagination, sorting, and filtering behind sensible defaults.

2. **Copy-paste variation**: The `renderCell` logic has three nearly identical branches for string, number, and date types, each with the same null-check and formatting wrapper. A single `formatCell(value, type)` function would eliminate 40 lines.

3. **Naming theater**: `useDataTableInternalStateManagement` (a custom hook that manages... state). Just call it `useTableState`.

**Overall Score: 2.3 / 5** -- Functional but the interface complexity will make this component hard to use correctly. The highest-impact refactor is collapsing the 14 props into a deep API: `<DataTable columns={cols} data={rows} />` with everything else as optional overrides.
