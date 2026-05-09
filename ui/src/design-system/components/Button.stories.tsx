import type { Meta, StoryObj } from "@storybook/react";
import { Button } from "./Button";
import { IconCheck, IconChain } from "./icons";

const meta: Meta<typeof Button> = {
  title: "Primitives/Button",
  component: Button,
  tags: ["autodocs"],
  args: { children: "Acknowledge" },
  argTypes: {
    variant: { control: "select", options: ["primary", "secondary", "danger", "ghost", "stamp"] },
    size: { control: "select", options: ["sm", "md", "lg"] },
  },
};
export default meta;

type S = StoryObj<typeof Button>;

export const Primary: S = { args: { variant: "primary" } };
export const Secondary: S = { args: { variant: "secondary" } };
export const Ghost: S = { args: { variant: "ghost" } };
export const Danger: S = { args: { variant: "danger", children: "Mark as anomaly" } };

/**
 * Reserve `stamp` for integrity-action CTAs (Anchor batch, Confirm reveal).
 * Never use it for routine clicks — orange burns out fast.
 */
export const Stamp: S = {
  args: { variant: "stamp", children: "Anchor batch", leadingIcon: <IconChain /> },
};

export const Sizes: S = {
  render: () => (
    <div className="vg-row">
      <Button size="sm">Small</Button>
      <Button size="md">Medium</Button>
      <Button size="lg">Large</Button>
    </div>
  ),
};

export const WithIcons: S = {
  args: { leadingIcon: <IconCheck />, children: "Acknowledge alert" },
};

export const Disabled: S = { args: { disabled: true } };
