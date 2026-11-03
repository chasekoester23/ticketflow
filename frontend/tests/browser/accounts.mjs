// Mirrors backend/app/permissions.py so browser stubs match the real API contract.
const PERMISSIONS = {
  "records.read": ["owner", "manager", "clerk", "viewer"],
  "tickets.upload": ["owner", "manager", "clerk"],
  "tickets.edit": ["owner", "manager", "clerk"],
  "tickets.flag": ["owner", "manager", "clerk"],
  "tickets.verify": ["owner", "manager"],
  "tickets.skip": ["owner", "manager"],
  "tickets.price_override": ["owner", "manager"],
  "projects.manage": ["owner", "manager"],
  "pricing.manage": ["owner", "manager"],
  "invoices.manage": ["owner", "manager"],
  "team.manage": ["owner"],
};
const LABELS = { owner: "Owner", manager: "Manager", clerk: "Clerk", viewer: "Viewer" };

export function profile(role = "owner", overrides = {}) {
  return {
    id: 1, username: "owner", display_name: null, role, role_label: LABELS[role],
    permissions: Object.keys(PERMISSIONS).filter((p) => PERMISSIONS[p].includes(role)).sort(),
    must_change_password: false, organization_name: "Original workspace",
    ...overrides,
  };
}
