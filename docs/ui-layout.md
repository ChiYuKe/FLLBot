# Console layout conventions

Reference study (2026-11-06):

- Ant Design ProComponents: https://github.com/ant-design/pro-components
- ProTable: https://pro-components.antdigital.dev/components/table
- shadcn/ui official dashboard: https://ui.shadcn.com/examples/dashboard
- Vben Admin: https://github.com/vbenjs/vue-vben-admin

The console applies these patterns with its existing HTML and JavaScript. No vendor code or framework is imported.

1. Resource pages use one page title and primary action row. Supplementary scope information remains below it; repeat card titles are hidden on primary surfaces.
2. List pages separate query controls, selection/batch actions, data, and pagination. Descriptions expand in place.
3. Chat configuration uses two columns on wide screens and grouped rows on smaller screens. DOM and keyboard order stay unchanged.
4. Surface, border, focus, selection, table, and field styles share existing light/dark theme tokens.
5. Content follows page scrolling. Dialogs lock the background and own one scroll area. Narrow tables may scroll horizontally.
6. Moving page actions retains the existing elements, IDs, event handlers and form ownership. It does not change API behavior or authorization.

`admin-layout.css` contains shared density and scroll rules. `workbench.css` contains page composition and visual hierarchy. `admin-layout.js` places primary resource actions in the shared header and updates their visibility on navigation.
