import type { Meta, StoryObj } from "@storybook/react";
import { EmptyState, ErrorState, LoadingState } from "./States";
import { Button } from "./Button";

const meta: Meta = {
  title: "States",
  tags: ["autodocs"],
};
export default meta;

export const Empty: StoryObj<typeof EmptyState> = {
  render: () => (
    <EmptyState
      title="No anomalies in the last 24 hours"
      body="The detector is running. New anomalies will appear here, sorted by severity."
      action={<Button variant="secondary" size="sm">Open detector settings</Button>}
    />
  ),
};

export const Error: StoryObj<typeof ErrorState> = {
  render: () => (
    <ErrorState
      title="Could not reach Anthropic management API"
      body="Last successful sync 14m ago. Anchored data is still readable; live ingestion is paused."
      action={<Button variant="secondary" size="sm">Retry now</Button>}
    />
  ),
};

export const Loading: StoryObj<typeof LoadingState> = {
  render: () => <LoadingState rows={5} />,
};

export const LoadingInline: StoryObj<typeof LoadingState> = {
  render: () => <LoadingState inline />,
};
