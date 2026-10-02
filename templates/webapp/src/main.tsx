// The page's entry: one store on the server's GET route, one API on its routes.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { createApi } from "./api";
import { createStore } from "./store";
import "./index.css";

const root = document.getElementById("root");
if (root === null) throw new Error("the page has no #root to draw in");

const api = createApi();
const store = createStore(api.read);
void store.load();

createRoot(root).render(
  <StrictMode>
    <App store={store} send={api.send} />
  </StrictMode>,
);
