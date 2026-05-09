import type { Preview } from "@storybook/react";

import "../src/design-system/tokens.css";
import "../src/design-system/components/styles.css";

const preview: Preview = {
  parameters: {
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
    backgrounds: {
      default: "vargate",
      values: [
        { name: "vargate", value: "var(--color-bg-1, #0b0d10)" },
        { name: "light", value: "#ffffff" },
      ],
    },
  },
};

export default preview;
