variable "prefix" {
  description = "Name prefix for every resource."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.prefix))
    error_message = "prefix must be 2-10 lowercase alphanumerics starting with a letter."
  }
}

variable "redirect_uri" {
  description = "The platform's callback: <APP_FRONTEND_URL>/api/v1/auth/oidc/callback (APP_OIDC_REDIRECT_URI)."
  type        = string

  validation {
    condition     = can(regex("^(https://|http://localhost[:/]).*/api/v1/auth/oidc/callback$", var.redirect_uri))
    error_message = "redirect_uri must be https (or http://localhost) and end in /api/v1/auth/oidc/callback."
  }
}

variable "logout_url" {
  description = "Front-channel logout URL, usually <APP_FRONTEND_URL>/login. Optional."
  type        = string
  default     = null
}

variable "group_claim" {
  description = "Which groups go into the token: SecurityGroup (all) or ApplicationGroup (assigned to the app only)."
  type        = string
  default     = "SecurityGroup"

  validation {
    condition     = contains(["SecurityGroup", "ApplicationGroup"], var.group_claim)
    error_message = "group_claim must be SecurityGroup or ApplicationGroup."
  }
}

variable "groups" {
  description = "Security groups to create, keyed by a short name, with member object ids (users)."
  type = map(object({
    description = string
    members     = list(string)
  }))
  default = {
    annotators = {
      description = "Annotide: annotator role in mapped projects"
      members     = []
    }
    reviewers = {
      description = "Annotide: reviewer role in mapped projects"
      members     = []
    }
    admins = {
      description = "Annotide: superusers (APP_OIDC_ADMIN_GROUPS)"
      members     = []
    }
  }
}
