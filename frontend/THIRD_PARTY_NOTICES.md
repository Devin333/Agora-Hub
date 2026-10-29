# Third-party notices

## Rakazo

- Source: https://github.com/elie222/rakazo
- License: Apache License 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

The application-wide visual language is adapted from Rakazo's interface: the
light and dark color tokens in `src/styles/theme-tokens.css`, the radius and
type scale, hairline borders, floating pill controls, the list-column sidebar,
the chat bubble and composer treatment, the settings dialog layout and the
sign-in layout. They are applied through `src/components/ui` primitives and the
feature stylesheets that consume the tokens, in both the browser portal and the
desktop app. No Rakazo source files are copied; the recipes were re-implemented
for this project's components.
