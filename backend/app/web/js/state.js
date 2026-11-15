// The page's state. Nothing is kept in the browser between visits: it all comes from the API.

export const SECTIONS = {
  revision: "Revisión",
  nuevo: "Nuevo proyecto",
  presupuesto: "Presupuesto y topes",
  generales: "Incidencias generales",
  ajustes: "Ajustes",
};

export const state = {
  settings: null,
  projects: [],
  key: null, // selected project
  section: "revision",
  review: null, // GET /projects/{key}/review of the selected project
  counts: null,
  budget: null,
  selected: null, // expense id shown in the detail
  tab: "pending",
  query: "",
  saving: false,
};

export const project = () => state.projects.find((p) => p.key === state.key) || null;
