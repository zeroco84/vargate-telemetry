import * as React from "react";

/** Tiny stroke-only icon set. 16×16 viewBox; inherit currentColor. */

type IconProps = React.SVGProps<SVGSVGElement> & { size?: number };

const base = (props: IconProps) => ({
  width: props.size ?? 16,
  height: props.size ?? 16,
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.4,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  ...props,
});

export const IconLock = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="7" width="10" height="7" rx="1.2" />
    <path d="M5 7V5a3 3 0 0 1 6 0v2" />
  </svg>
);

export const IconUnlock = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="7" width="10" height="7" rx="1.2" />
    <path d="M5 7V5a3 3 0 0 1 5.7-1.3" />
  </svg>
);

export const IconCheck = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 8.5 L6.5 12 L13 4.5" />
  </svg>
);

export const IconX = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 4 L12 12 M12 4 L4 12" />
  </svg>
);

export const IconChevronRight = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6 3 L11 8 L6 13" />
  </svg>
);

export const IconChevronDown = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 6 L8 11 L13 6" />
  </svg>
);

export const IconInfo = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="8" cy="8" r="6" />
    <path d="M8 7v4 M8 5v0.01" />
  </svg>
);

export const IconAlert = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M8 2 L14 13 H2 Z" />
    <path d="M8 6v3 M8 11v0.01" />
  </svg>
);

export const IconShieldX = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M8 2 L13 4 V8 C13 11 10.5 13.5 8 14 C5.5 13.5 3 11 3 8 V4 Z" />
    <path d="M6 7 L10 11 M10 7 L6 11" />
  </svg>
);

export const IconEye = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2 8 C4 4.5 6 3 8 3 C10 3 12 4.5 14 8 C12 11.5 10 13 8 13 C6 13 4 11.5 2 8 Z" />
    <circle cx="8" cy="8" r="2" />
  </svg>
);

export const IconEyeOff = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 13 L13 3" />
    <path d="M5 11.5 C3.5 10.5 2.5 9.3 2 8 C4 4.5 6 3 8 3 C9 3 10 3.3 11 4" />
    <path d="M11 11 C10 11.7 9 12 8 12 C7.5 12 7 11.95 6.5 11.85" />
  </svg>
);

export const IconSearch = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="7" cy="7" r="4" />
    <path d="M10 10 L13.5 13.5" />
  </svg>
);

export const IconInbox = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="2" y="3" width="12" height="10" rx="1.2" />
    <path d="M2 9 H5 L6 11 H10 L11 9 H14" />
  </svg>
);

export const IconSpinner = (p: IconProps) => (
  <svg {...base(p)} style={{ animation: "vg-spin 0.9s linear infinite", ...(p.style ?? {}) }}>
    <path d="M8 2 A6 6 0 0 1 14 8" />
  </svg>
);

export const IconChain = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6.5 9.5 L9.5 6.5" />
    <path d="M5 11 L4 12 A2.1 2.1 0 0 1 4 9 L6 7" />
    <path d="M11 5 L12 4 A2.1 2.1 0 0 1 12 7 L10 9" />
  </svg>
);

export const IconClose = IconX;
