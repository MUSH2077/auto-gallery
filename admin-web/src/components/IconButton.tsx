import { type ButtonHTMLAttributes, type ReactNode } from "react";

export default function IconButton({
  label,
  children,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  children: ReactNode;
}) {
  return (
    <button {...props} aria-label={label} title={props.title || label} className={`btn-icon ${className}`}>
      {children}
    </button>
  );
}
