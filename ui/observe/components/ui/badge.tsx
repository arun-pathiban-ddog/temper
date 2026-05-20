import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-md px-2 py-0.5 text-xs font-medium whitespace-nowrap transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 [&>svg]:pointer-events-none [&>svg]:size-3",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground [a&]:hover:bg-primary/90",
        secondary:
          "bg-secondary text-secondary-foreground [a&]:hover:bg-secondary/90",
        destructive:
          "bg-destructive text-white focus-visible:ring-destructive/20 dark:bg-destructive/60 dark:focus-visible:ring-destructive/40 [a&]:hover:bg-destructive/90",
        outline:
          "border border-border text-foreground [a&]:hover:bg-accent [a&]:hover:text-accent-foreground",
        ghost: "[a&]:hover:bg-accent [a&]:hover:text-accent-foreground",
        link: "text-primary underline-offset-4 [a&]:hover:underline",
        success: "bg-chart-3/10 text-chart-3",
        danger: "bg-destructive/10 text-destructive",
        warning: "bg-amber-500/15 text-amber-400",
        info: "bg-blue-500/20 text-blue-400",
        muted: "bg-muted text-muted-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

const DOT_COLORS: Partial<
  Record<NonNullable<VariantProps<typeof badgeVariants>["variant"]>, string>
> = {
  success: "bg-chart-3",
  danger: "bg-destructive",
  muted: "bg-muted-foreground",
  warning: "bg-amber-400",
  info: "bg-blue-400",
}

function Badge({
  className,
  variant = "default",
  dot = false,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & {
    asChild?: boolean
    /** Render as a small colored dot instead of a full badge. */
    dot?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "span"

  if (dot) {
    const dotBg = (variant && DOT_COLORS[variant]) ?? "bg-primary"
    return (
      <Comp
        data-slot="badge"
        data-variant={variant}
        className={cn(
          "inline-flex size-2 shrink-0 rounded-full p-0 min-w-0 min-h-0",
          dotBg,
          className,
        )}
        {...props}
      />
    )
  }

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  )
}

export { Badge, badgeVariants }
